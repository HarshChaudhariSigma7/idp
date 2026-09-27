"""Deterministic cross-validation. The model never grades its own arithmetic: we recompute it.
Every failed check names the fields it implicates and explains itself in plain language."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import product

from sereno.extraction.normalize import IFSC_RE, VEHICLE_RE, HSN_RE, today
from sereno.extraction.result import ExtractedDoc, line_key
from sereno.validation.gst import GST_RATES, STATE_CODES, gstin_problem, state_code_for, words_to_amount


@dataclass
class CheckResult:
    check_id: str
    status: str               # pass | fail | skip
    message: str              # plain language, safe to show a finance user
    field_keys: list[str] = field(default_factory=list)
    severity: str = "error"   # error: arithmetic/compliance => whole-doc penalty; warn: needs a look
    details: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def _d(x) -> Decimal | None:
    return None if x is None else Decimal(str(x))


def _inr(x) -> str:
    """Indian digit grouping for messages: 1,23,456.00"""
    if x is None:
        return "-"
    q = Decimal(str(x)).quantize(Decimal("0.01"))
    neg = q < 0
    s = f"{abs(q):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if neg else ''}₹{whole}.{frac}"


class Tol:
    def __init__(self, abs_tol: float, rel_tol: float):
        self.abs, self.rel = Decimal(str(abs_tol)), Decimal(str(rel_tol))

    def eq(self, a: Decimal, b: Decimal, abs_override: Decimal | None = None) -> bool:
        lim = max(abs_override if abs_override is not None else self.abs, self.rel * max(abs(a), abs(b)))
        return abs(a - b) <= lim


LINE_TOL = Decimal("0.51")


def run_checks(doc: ExtractedDoc, abs_tol: float = 1.0, rel_tol: float = 0.00002) -> list[CheckResult]:
    tol = Tol(abs_tol, rel_tol)
    checks = [_required(doc)]
    dt = doc.spec.doc_type
    if dt == "invoice":
        checks += _invoice(doc, tol)
    elif dt == "lr":
        checks += _lr(doc, tol)
    elif dt == "po":
        checks += _po(doc, tol)
    elif dt == "grn":
        checks += _grn(doc)
    elif dt == "contract":
        checks += _contract(doc)
    checks += _formats(doc)
    return [c for c in checks if c is not None]


# --- generic -------------------------------------------------------------------------------------

def _required(doc: ExtractedDoc) -> CheckResult:
    missing = [f for f in doc.spec.fields if f.required and doc.v(f.name) is None]
    keys = [f.name for f in missing]
    if doc.spec.line_fields and not doc.lines:
        keys.append("line_items")
    if not keys:
        return CheckResult("required_fields", "pass", "All required fields were found")
    labels = [f.label for f in missing] + (["line items"] if "line_items" in keys else [])
    return CheckResult("required_fields", "fail", f"Couldn't find: {', '.join(labels)}", keys, "warn")


def _formats(doc: ExtractedDoc) -> list[CheckResult]:
    out = []
    for f in doc.all_fields():
        if f.value is None:
            continue
        t = f.spec.type
        problem = None
        if t == "gstin":
            problem = gstin_problem(f.value)
        elif t == "vehicle" and not VEHICLE_RE.match(f.value):
            problem = "Vehicle number doesn't look like a valid Indian registration (e.g. MH12AB1234)"
        elif t == "ifsc" and not IFSC_RE.match(f.value):
            problem = "IFSC should be 11 characters like HDFC0001234"
        elif t == "hsn" and not HSN_RE.match(f.value):
            problem = "HSN/SAC code should be 4, 6 or 8 digits"
        elif f.spec.name == "eway_bill_number" and not (str(f.value).replace(" ", "").isdigit()
                                                        and len(str(f.value).replace(" ", "")) == 12):
            problem = "E-way bill number should be 12 digits"
        elif f.spec.name == "irn" and not (len(str(f.value)) == 64 and all(c in "0123456789abcdefABCDEF" for c in str(f.value))):
            problem = "IRN should be a 64-character code"
        if t in ("gstin", "vehicle", "ifsc", "hsn") or f.spec.name in ("eway_bill_number", "irn"):
            f.format_ok = problem is None
        if problem:
            out.append(CheckResult(f"format:{f.key}", "fail", f"{f.spec.label}: {problem}", [f.key], "error"))
    return out


def _date_sanity(doc: ExtractedDoc, name: str) -> CheckResult | None:
    v = doc.v(name)
    if v is None:
        return None
    d = date.fromisoformat(v)
    t = today()
    label = doc.header[name].spec.label
    if d > t + timedelta(days=1):
        return CheckResult(f"date:{name}", "fail", f"{label} is in the future ({d:%d %b %Y}). Day and month may be swapped.",
                           [name], "error")
    if d < t - timedelta(days=365 * 3):
        return CheckResult(f"date:{name}", "fail", f"{label} is more than 3 years old ({d:%d %b %Y})", [name], "warn")
    return CheckResult(f"date:{name}", "pass", f"{label} looks valid")


def _sum(vals) -> Decimal:
    return sum((v for v in vals if v is not None), Decimal("0"))


# --- invoice -------------------------------------------------------------------------------------

def _invoice(doc: ExtractedDoc, tol: Tol) -> list[CheckResult]:
    out: list[CheckResult | None] = []
    lines = doc.lines
    for i, ln in enumerate(lines):
        g = lambda n: _d(ln[n].value) if n in ln else None  # noqa: E731
        qty, rate, disc, taxable = g("quantity"), g("rate"), g("discount") or Decimal("0"), g("taxable_value")
        if qty is not None and rate is not None and taxable is not None:
            calc = qty * rate - disc
            ok = abs(calc - taxable) <= max(LINE_TOL, abs(taxable) * Decimal("0.0005"))
            out.append(CheckResult(f"line_math:{i}", "pass" if ok else "fail",
                                   f"Line {i + 1}: quantity × rate {'matches' if ok else 'does not match'} the line amount"
                                   + ("" if ok else f" ({qty} × {rate} = {_inr(calc)} but line shows {_inr(taxable)})"),
                                   [line_key(i, n) for n in ("quantity", "rate", "discount", "taxable_value")],
                                   details={"calc": str(calc), "printed": str(taxable)}))
        rate_pct = g("gst_rate")
        line_tax = [g("cgst_amount"), g("sgst_amount"), g("igst_amount")]
        if taxable is not None and rate_pct is not None and any(x is not None for x in line_tax):
            calc = taxable * rate_pct / 100
            got = _sum(line_tax)
            ok = abs(calc - got) <= max(LINE_TOL, got * Decimal("0.001"))
            out.append(CheckResult(f"line_tax:{i}", "pass" if ok else "fail",
                                   f"Line {i + 1}: GST {rate_pct}% of {_inr(taxable)} "
                                   + ("matches" if ok else f"should be {_inr(calc)} but line shows {_inr(got)}"),
                                   [line_key(i, n) for n in ("taxable_value", "gst_rate", "cgst_amount", "sgst_amount", "igst_amount")]))
        if rate_pct is not None and rate_pct not in GST_RATES:
            out.append(CheckResult(f"gst_slab:{i}", "fail", f"Line {i + 1}: {rate_pct}% is not a valid GST rate",
                                   [line_key(i, "gst_rate")], "error"))
        total = g("line_total")
        if total is not None and taxable is not None:
            tax = _sum(line_tax)
            ok = abs(total - taxable) <= LINE_TOL or (tax > 0 and abs(total - taxable - tax) <= LINE_TOL)
            out.append(CheckResult(f"line_total:{i}", "pass" if ok else "fail",
                                   f"Line {i + 1}: line total {'is consistent' if ok else 'does not add up'}",
                                   [line_key(i, n) for n in ("taxable_value", "line_total", "cgst_amount", "sgst_amount", "igst_amount")]))

    subtotal, gt = _d(doc.v("subtotal")), _d(doc.v("grand_total"))
    cgst, sgst, igst = _d(doc.v("cgst_amount")), _d(doc.v("sgst_amount")), _d(doc.v("igst_amount"))
    cess, tcs, oc = _d(doc.v("cess_amount")), _d(doc.v("tcs_amount")), _d(doc.v("other_charges"))
    disc, ro = _d(doc.v("discount_total")), _d(doc.v("round_off"))
    tax_total = _sum([cgst, sgst, igst])

    # Line items sum to subtotal (allowing for charges/discount being inside or outside the lines)
    line_taxables = [_d(ln["taxable_value"].value) for ln in lines if "taxable_value" in ln]
    if lines and subtotal is not None and all(x is not None for x in line_taxables):
        s = _sum(line_taxables)
        variants = {s, s + (oc or 0), s - (disc or 0), s + (oc or 0) - (disc or 0)}
        ok = any(tol.eq(v, subtotal) for v in variants)
        out.append(CheckResult("lines_sum_subtotal", "pass" if ok else "fail",
                               "Line items add up to the taxable value" if ok else
                               f"Line items add up to {_inr(s)} but taxable value shows {_inr(subtotal)}",
                               ["subtotal"] + [line_key(i, "taxable_value") for i in range(len(lines))],
                               details={"lines_sum": str(s), "subtotal": str(subtotal)}))

    for head, name in (("cgst_amount", "CGST"), ("sgst_amount", "SGST"), ("igst_amount", "IGST")):
        per_line = [_d(ln[head].value) for ln in lines if head in ln and ln[head].value is not None]
        hv = _d(doc.v(head))
        if per_line and hv is not None:
            s = _sum(per_line)
            ok = tol.eq(s, hv)
            out.append(CheckResult(f"tax_sum:{head}", "pass" if ok else "fail",
                                   f"{name} on lines {'matches' if ok else 'does not match'} the {name} total"
                                   + ("" if ok else f" ({_inr(s)} vs {_inr(hv)})"),
                                   [head] + [line_key(i, head) for i in range(len(lines))]))

    # Tax-rate reconciliation
    if subtotal is not None and subtotal > 0 and (cgst is not None or igst is not None):
        rated = [(_d(ln["taxable_value"].value), _d(ln["gst_rate"].value)) for ln in lines
                 if "gst_rate" in ln and ln["taxable_value"].value is not None and ln["gst_rate"].value is not None]
        if rated and len(rated) == len(lines):
            expected = sum((t * r / 100 for t, r in rated), Decimal("0"))
            base_extra = subtotal - _sum(t for t, _ in rated)
            if base_extra > 0 and rated:  # charges taxed at the highest line rate (common practice)
                expected += base_extra * max(r for _, r in rated) / 100
            ok = tol.eq(expected, tax_total, abs_override=Decimal("2"))
            out.append(CheckResult("tax_rate_reconcile", "pass" if ok else "fail",
                                   "GST amounts reconcile with the line tax rates" if ok else
                                   f"GST at the printed rates should be {_inr(expected)} but invoice shows {_inr(tax_total)}",
                                   ["cgst_amount", "sgst_amount", "igst_amount", "subtotal"]))
        else:
            eff = (tax_total / subtotal * 100).quantize(Decimal("0.01"))
            ok = any(abs(eff - r) <= Decimal("0.05") for r in GST_RATES)
            out.append(CheckResult("tax_rate_reconcile", "pass" if ok else "fail",
                                   f"Effective GST rate {eff}% is a standard rate" if ok else
                                   f"Effective GST rate works out to {eff}%, which is not a standard GST rate "
                                   "(could be mixed rates; please check the tax amounts)",
                                   ["cgst_amount", "sgst_amount", "igst_amount", "subtotal"], "warn" if lines else "error"))

    # CGST must equal SGST
    if cgst is not None and sgst is not None and (cgst or sgst):
        ok = abs(cgst - sgst) <= Decimal("0.01") * 2
        out.append(CheckResult("cgst_eq_sgst", "pass" if ok else "fail",
                               "CGST equals SGST" if ok else f"CGST ({_inr(cgst)}) and SGST ({_inr(sgst)}) should be equal",
                               ["cgst_amount", "sgst_amount"]))

    # Intra-state => CGST+SGST, inter-state => IGST
    sup = doc.v("supplier_gstin")
    sup_state = sup[:2] if sup and sup[:2] in STATE_CODES else None
    pos_state = state_code_for(doc.v("place_of_supply"))
    if pos_state is None:
        buyer = doc.v("buyer_gstin")
        pos_state = buyer[:2] if buyer and buyer[:2] in STATE_CODES else None
    if sup_state and pos_state and (tax_total > 0):
        intra = sup_state == pos_state
        has_cs = bool((cgst or 0) > 0 or (sgst or 0) > 0)
        has_i = bool((igst or 0) > 0)
        ok = (intra and has_cs and not has_i) or (not intra and has_i and not has_cs)
        msg = ("Tax type matches the supply (same state: CGST+SGST; different state: IGST)" if ok else
               ("Supplier and place of supply are in the same state, so tax should be CGST+SGST, not IGST" if intra
                else "Supplier and place of supply are in different states, so tax should be IGST, not CGST+SGST"))
        out.append(CheckResult("tax_type_vs_state", "pass" if ok else "fail", msg,
                               ["cgst_amount", "sgst_amount", "igst_amount", "place_of_supply", "supplier_gstin"]))

    # Grand total composition
    if gt is not None and subtotal is not None:
        found = None
        for oc_in_sub, disc_in_sub in product((False, True), repeat=2):
            calc = subtotal + tax_total + (cess or 0) + (tcs or 0) + (ro or 0)
            if not oc_in_sub:
                calc += oc or 0
            if not disc_in_sub:
                calc -= disc or 0
            if tol.eq(calc, gt):
                found = calc
                break
        if found is None:
            calc = subtotal + tax_total + (cess or 0) + (tcs or 0) + (ro or 0) + (oc or 0) - (disc or 0)
        ok = found is not None
        out.append(CheckResult("grand_total", "pass" if ok else "fail",
                               "Taxable value + GST + other charges = invoice total" if ok else
                               f"Taxable value + GST + charges comes to {_inr(calc)} but the invoice total shows {_inr(gt)}",
                               ["grand_total", "subtotal", "cgst_amount", "sgst_amount", "igst_amount", "cess_amount",
                                "tcs_amount", "other_charges", "discount_total", "round_off"],
                               details={"calc": str(calc), "printed": str(gt)}))
    if ro is not None:
        ok = abs(ro) < 1
        out.append(CheckResult("round_off", "pass" if ok else "fail",
                               "Round-off is under ₹1" if ok else f"Round-off of {_inr(ro)} is too large (should be under ₹1)",
                               ["round_off"]))
    words = doc.v("amount_in_words")
    if words and gt is not None:
        w = words_to_amount(words)
        if w is None:
            out.append(CheckResult("amount_in_words", "skip", "Amount in words could not be interpreted automatically",
                                   ["amount_in_words"], "warn"))
        else:
            ok = abs(w - gt) < 1
            out.append(CheckResult("amount_in_words", "pass" if ok else "fail",
                                   "Amount in words matches the total" if ok else
                                   f"Amount in words says {_inr(w)} but the total shows {_inr(gt)}",
                                   ["amount_in_words", "grand_total"]))
    sg, bg = doc.v("supplier_gstin"), doc.v("buyer_gstin")
    if sg and bg and sg == bg:
        out.append(CheckResult("gstin_distinct", "fail", "Vendor and buyer GSTIN are the same; one was probably read from the wrong box",
                               ["supplier_gstin", "buyer_gstin"]))
    out.append(_date_sanity(doc, "invoice_date"))
    inv_d, due = doc.v("invoice_date"), doc.v("due_date")
    if inv_d and due and due < inv_d:
        out.append(CheckResult("due_after_invoice", "fail", "Due date is before the invoice date", ["due_date", "invoice_date"], "warn"))
    return [c for c in out if c is not None]


# --- LR ------------------------------------------------------------------------------------------

def _lr(doc: ExtractedDoc, tol: Tol) -> list[CheckResult]:
    out: list[CheckResult | None] = []
    cw, aw = _d(doc.v("charged_weight_kg")), _d(doc.v("actual_weight_kg"))
    rate, fr = _d(doc.v("freight_rate")), _d(doc.v("freight_amount"))
    oc, total = _d(doc.v("other_charges")), _d(doc.v("total_freight"))
    pk = _d(doc.v("packages_count"))
    if cw is not None and aw is not None and cw < aw:
        out.append(CheckResult("charged_ge_actual", "fail", "Charged weight is less than actual weight; one of the weights may be misread",
                               ["charged_weight_kg", "actual_weight_kg"], "warn"))
    if rate is not None and fr is not None:
        bases = []
        w = cw if cw is not None else aw
        if w is not None:
            bases += [w * rate, w / 100 * rate, w / 1000 * rate]  # per kg, per quintal, per tonne
        if pk is not None:
            bases.append(pk * rate)
        bases.append(rate)  # fixed / per trip
        ok = any(tol.eq(b, fr) for b in bases)
        out.append(CheckResult("freight_math", "pass" if ok else "fail",
                               "Freight = weight/packages × rate" if ok else
                               "Freight doesn't match weight or packages × rate (per kg, quintal, tonne or package)",
                               ["freight_rate", "freight_amount", "charged_weight_kg", "packages_count"], "warn"))
    if total is not None and fr is not None:
        calc = fr + (oc or 0)
        ok = tol.eq(calc, total)
        out.append(CheckResult("total_freight", "pass" if ok else "fail",
                               "Basic freight + other charges = total freight" if ok else
                               f"Basic freight + other charges comes to {_inr(calc)} but total shows {_inr(total)}",
                               ["total_freight", "freight_amount", "other_charges"]))
    c1, c2 = doc.v("consignor_gstin"), doc.v("consignee_gstin")
    if c1 and c2 and c1 == c2:
        out.append(CheckResult("gstin_distinct", "fail", "Consignor and consignee GSTIN are identical; check both boxes",
                               ["consignor_gstin", "consignee_gstin"], "warn"))
    out.append(_date_sanity(doc, "lr_date"))
    return [c for c in out if c is not None]


# --- PO / GRN / contract -------------------------------------------------------------------------

def _po(doc: ExtractedDoc, tol: Tol) -> list[CheckResult]:
    out: list[CheckResult | None] = []
    amounts = []
    for i, ln in enumerate(doc.lines):
        q, r, amt = _d(ln["quantity"].value), _d(ln["rate"].value), _d(ln["line_total"].value)
        disc = _d(ln["discount"].value) or Decimal("0")
        amounts.append(amt)
        if q is not None and r is not None and amt is not None:
            calc = q * r - disc
            ok = abs(calc - amt) <= max(LINE_TOL, abs(amt) * Decimal("0.0005"))
            out.append(CheckResult(f"line_math:{i}", "pass" if ok else "fail",
                                   f"Line {i + 1}: quantity × rate {'matches' if ok else 'does not match'} the amount",
                                   [line_key(i, n) for n in ("quantity", "rate", "discount", "line_total")]))
    sub, gt = _d(doc.v("subtotal")), _d(doc.v("grand_total"))
    if doc.lines and sub is not None and all(a is not None for a in amounts):
        s = _sum(amounts)
        ok = tol.eq(s, sub)
        out.append(CheckResult("lines_sum_subtotal", "pass" if ok else "fail",
                               "Line amounts add up to the PO value" if ok else f"Lines add up to {_inr(s)} but PO shows {_inr(sub)}",
                               ["subtotal"] + [line_key(i, "line_total") for i in range(len(doc.lines))]))
    if sub is not None and gt is not None:
        calc = sub + _sum([_d(doc.v(n)) for n in ("cgst_amount", "sgst_amount", "igst_amount", "other_charges")])
        ok = tol.eq(calc, gt)
        out.append(CheckResult("grand_total", "pass" if ok else "fail",
                               "PO total adds up" if ok else f"PO value + taxes comes to {_inr(calc)} but total shows {_inr(gt)}",
                               ["grand_total", "subtotal", "cgst_amount", "sgst_amount", "igst_amount", "other_charges"]))
    out.append(_date_sanity(doc, "po_date"))
    return [c for c in out if c is not None]


def _grn(doc: ExtractedDoc) -> list[CheckResult]:
    out: list[CheckResult | None] = []
    for i, ln in enumerate(doc.lines):
        rec, acc, rej = (_d(ln[n].value) for n in ("quantity_received", "quantity_accepted", "quantity_rejected"))
        if rec is not None and acc is not None:
            ok = abs(acc + (rej or 0) - rec) <= Decimal("0.001")
            out.append(CheckResult(f"accept_reject:{i}", "pass" if ok else "fail",
                                   f"Line {i + 1}: accepted + rejected {'=' if ok else '≠'} received",
                                   [line_key(i, n) for n in ("quantity_received", "quantity_accepted", "quantity_rejected")]))
    out.append(_date_sanity(doc, "grn_date"))
    return [c for c in out if c is not None]


def _contract(doc: ExtractedDoc) -> list[CheckResult]:
    eff, exp = doc.v("effective_date"), doc.v("expiry_date")
    if eff and exp and exp <= eff:
        return [CheckResult("term_dates", "fail", "Expiry date is not after the effective date",
                            ["effective_date", "expiry_date"])]
    return []
