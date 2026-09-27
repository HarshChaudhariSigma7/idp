"""Checks that use evidence beyond the page's own arithmetic:

* machine-readable codes (signed e-invoice QR, UPI QR) vs what was read from the print,
* the financial year embedded in document numbers (catches day/month swaps and proposes the fix),
* line-item counts across independent readings and the e-invoice QR (catches missing rows),
* company master data: the customer's own GSTINs, each vendor's history (name, bank account,
  invoice-number format) and duplicate detection.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from sereno.extraction.normalize import clean_code, shape_mask, today
from sereno.extraction.result import ExtractedDoc
from sereno.validation.checks import CheckResult, _inr
from sereno.validation.gst import gstin_problem

# Characters that look alike on scans, in both directions.
CONFUSABLE = {
    "0": "ODQ8", "O": "0DQ", "D": "0O", "Q": "0O", "1": "I7L", "I": "1L", "L": "1I", "7": "1T", "T": "7",
    "5": "S6", "S": "58", "8": "B3S0", "B": "8", "2": "Z", "Z": "2", "6": "G5", "G": "6C", "C": "G",
    "3": "8", "4": "A", "A": "4", "9": "g", "U": "V", "V": "U", "M": "N", "N": "M",
}


@dataclass
class VendorSnapshot:
    name: str | None = None
    bank_account: str | None = None      # normalised account number as last confirmed by a reviewer
    invoice_masks: dict = field(default_factory=dict)   # shape of invoice numbers -> count
    docs: int = 0


@dataclass
class CheckContext:
    own_gstins: set = field(default_factory=set)
    vendor: VendorSnapshot | None = None
    duplicates: list = field(default_factory=list)       # [{"created_at": datetime, "match": str}]


# --- machine-readable codes ----------------------------------------------------------------------

def code_checks(doc: ExtractedDoc) -> list[CheckResult]:
    out = []
    for fv in doc.header.values():
        if fv.code_source is None or fv.code_filled:
            continue
        label = fv.spec.label
        if fv.code_source == "einvoice_qr":
            if fv.code_agrees:
                out.append(CheckResult(f"qr:{fv.key}", "pass", f"{label} matches the signed e-invoice QR", [fv.key]))
            else:
                out.append(CheckResult(f"qr:{fv.key}", "fail",
                                       f"Printed {label} ({_show(fv.value)}) differs from the signed e-invoice QR "
                                       f"({_show(fv.code_value)}): a misread, or an edited document",
                                       [fv.key], "error", {"qr": _show(fv.code_value)}))
        elif fv.code_source == "upi_qr" and fv.key == "grand_total":
            out.append(CheckResult("upi_amount", "pass" if fv.code_agrees else "fail",
                                   "Total matches the UPI payment QR" if fv.code_agrees else
                                   f"UPI payment QR asks for {_inr(fv.code_value)} but the total reads {_inr(fv.value)} "
                                   "(could be a part payment, or a misread total)", [fv.key], "warn"))
    return out


def _show(v) -> str:
    return _inr(v) if isinstance(v, float) else str(v)


# --- financial year in document numbers ------------------------------------------------------------

_FY_PAIRS = {"invoice": ("invoice_number", "invoice_date"), "po": ("po_number", "po_date"),
             "lr": ("lr_number", "lr_date"), "grn": ("grn_number", "grn_date")}
_FY_RE = re.compile(r"(?<!\d)(?:20)?(\d{2})\s*[-/]\s*(?:20)?(\d{2})(?!\d)")


def financial_year(number: str | None) -> tuple[date, date] | None:
    """'INV/24-25/0412' -> (2024-04-01, 2025-03-31). Only when the two years are consecutive."""
    if not number:
        return None
    for m in _FY_RE.finditer(str(number)):
        a, b = int(m.group(1)), int(m.group(2))
        if b == (a + 1) % 100 and 15 <= a <= (today().year % 100) + 1:
            return date(2000 + a, 4, 1), date(2000 + a + 1, 3, 31)
    return None


def fy_checks(doc: ExtractedDoc) -> list[CheckResult]:
    pair = _FY_PAIRS.get(doc.spec.doc_type)
    if not pair:
        return []
    num_f, date_f = pair
    fy, d = financial_year(doc.v(num_f)), doc.v(date_f)
    if fy is None or d is None:
        return []
    d = date.fromisoformat(d)
    start, end = fy
    fy_label = f"FY {start.year}-{str(end.year)[2:]}"
    if start <= d <= end:
        return [CheckResult("fy_date", "pass", f"Date falls inside {fy_label} printed in the number", [date_f])]
    swapped = None
    if d.day <= 12:
        try:
            swapped = date(d.year, d.day, d.month)
        except ValueError:
            swapped = None
    label = doc.header[date_f].spec.label
    if swapped and start <= swapped <= end:
        return [CheckResult("fy_date", "fail",
                            f"{label} {d:%d %b %Y} is outside {fy_label} printed in the number, but {swapped:%d %b %Y} fits: "
                            "day and month were probably swapped", [date_f], "error", {"suggest": swapped.isoformat()})]
    return [CheckResult("fy_date", "fail", f"{label} {d:%d %b %Y} is outside {fy_label} printed in the number",
                        [date_f, num_f], "warn")]


# --- line counts -----------------------------------------------------------------------------------

def row_count_checks(doc: ExtractedDoc) -> list[CheckResult]:
    if not doc.spec.line_fields:
        return []
    n = len(doc.lines)
    qr = doc.row_counts.get("qr")
    anchor = "subtotal" if "subtotal" in doc.header else next(iter(doc.header))
    if qr is not None:
        ok = qr == n
        return [CheckResult("line_count", "pass" if ok else "fail",
                            f"{n} line items, as the e-invoice QR states" if ok else
                            f"The e-invoice QR says {qr} line items but {n} were read: a row may be missing or split",
                            [anchor], "error")]
    b = doc.row_counts.get("secondary")
    if b is not None and b != n:
        return [CheckResult("line_count", "fail",
                            f"The two independent readings found {n} and {b} line items: a row may be missing or split",
                            [anchor], "error")]
    return []


# --- company master data ----------------------------------------------------------------------------

def gstin_near(a: str, b: str, max_diff: int = 2) -> bool:
    """Same GSTIN up to look-alike characters (a scan misread), not a different taxpayer."""
    if not a or not b or len(a) != len(b) or a == b:
        return False
    diffs = [(x, y) for x, y in zip(a, b) if x != y]
    return len(diffs) <= max_diff and all(y in CONFUSABLE.get(x, "") or x in CONFUSABLE.get(y, "") for x, y in diffs)


def gstin_repairs(g: str | None) -> list[str]:
    """Valid GSTINs one look-alike character away from an invalid reading."""
    if not g or gstin_problem(g) is None or len(g) != 15:
        return []
    out = []
    for i, ch in enumerate(g):
        for alt in CONFUSABLE.get(ch, ""):
            cand = g[:i] + alt + g[i + 1:]
            if gstin_problem(cand) is None:
                out.append(cand)
    return sorted(set(out))


def _norm_account(v) -> str | None:
    s = clean_code(v)
    return s or None


def master_checks(doc: ExtractedDoc, ctx: CheckContext) -> list[CheckResult]:
    out: list[CheckResult] = []
    dt = doc.spec.doc_type
    own_field = {"invoice": "buyer_gstin", "po": "buyer_gstin", "lr": None, "grn": None}.get(dt)
    if own_field and ctx.own_gstins and doc.v(own_field):
        g = doc.v(own_field)
        fv = doc.header[own_field]
        if g in ctx.own_gstins:
            fv.master_match = True
            fv.evidence.append("your company's GSTIN")
            out.append(CheckResult("own_gstin", "pass", "Billed to one of your GSTINs", [own_field]))
        else:
            near = [o for o in ctx.own_gstins if gstin_near(g, o)]
            if len(near) == 1:
                fv.master_match = False
                fv.suggested_value = near[0]
                fv.suggestion_reason = "your company's GSTIN differs only by look-alike characters"
                out.append(CheckResult("own_gstin", "fail", f"{fv.spec.label} reads {g}; your GSTIN is {near[0]} (probably misread)",
                                       [own_field], "error", {"suggest": near[0]}))
            else:
                out.append(CheckResult("own_gstin", "fail",
                                       f"Billed to {g}, which isn't one of your GSTINs: input tax credit may be at risk",
                                       [own_field], "warn"))
    v = ctx.vendor
    if v and dt == "invoice" and v.docs:
        name = doc.v("supplier_name")
        if v.name and name:
            from rapidfuzz import fuzz
            squash = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())  # noqa: E731
            if fuzz.ratio(squash(v.name), squash(name)) >= 80:
                doc.header["supplier_name"].master_match = True
                doc.header["supplier_gstin"].master_match = True
                doc.header["supplier_gstin"].evidence.append("known vendor")
            else:
                out.append(CheckResult("vendor_name", "fail",
                                       f"This GSTIN belonged to '{v.name}' on earlier invoices, but the vendor name reads "
                                       f"'{name}': the GSTIN may be misread", ["supplier_gstin", "supplier_name"], "warn"))
        acct = _norm_account(doc.v("bank_account_number"))
        if v.bank_account and acct and acct != v.bank_account:
            out.append(CheckResult("vendor_bank_changed", "fail",
                                   f"Bank account (ending {acct[-4:]}) differs from this vendor's previous invoices (ending "
                                   f"{v.bank_account[-4:]}). Confirm with the vendor by phone before paying: this is the most "
                                   "common invoice-fraud pattern", ["bank_account_number", "bank_ifsc"], "error"))
        num = doc.v("invoice_number")
        if num and sum(v.invoice_masks.values()) >= 3:
            mask = shape_mask(num)
            if mask not in v.invoice_masks:
                usual = max(v.invoice_masks, key=v.invoice_masks.get)
                out.append(CheckResult("invoice_number_format", "fail",
                                       f"Invoice number {num} doesn't follow this vendor's usual format ({usual}): check for a misread",
                                       ["invoice_number"], "warn"))
            else:
                doc.header["invoice_number"].evidence.append("usual format for this vendor")
    for dup in ctx.duplicates[:1]:
        when = dup["created_at"].strftime("%d %b %Y") if dup.get("created_at") else "earlier"
        out.append(CheckResult("duplicate", "fail", f"Possible duplicate: {dup['match']} as a document uploaded on {when}",
                               ["invoice_number" if dt == "invoice" else next(iter(doc.header))], "error"))
    return out
