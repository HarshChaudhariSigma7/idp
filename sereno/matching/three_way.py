"""3-way match: PO (what we ordered) x GRN (what we received/accepted) x invoice (what we're billed).
Every mismatch is explicit, per line, in rupees and units, in plain language."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from sereno.extraction.result import ExtractedDoc


@dataclass
class Tolerances:
    price_pct: float = 0.5     # invoice rate may exceed PO rate by at most this %
    qty_pct: float = 0.0       # over-billing tolerance vs accepted quantity
    amount_abs: float = 1.0


@dataclass
class Issue:
    kind: str       # price | quantity | tax | reference | supplier | missing_line | extra_line | total
    severity: str   # block | warn
    message: str
    line: int | None = None


@dataclass
class MatchResult:
    status: str
    summary: str
    lines: list[dict] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"status": self.status, "summary": self.summary, "lines": self.lines,
                "issues": [i.__dict__ for i in self.issues]}


def _v(line: dict, name: str):
    f = line.get(name)
    return None if f is None else f.value


def _line_key_score(a: dict, b: dict) -> float:
    ca, cb = _v(a, "item_code"), _v(b, "item_code")
    if ca and cb and str(ca).strip().upper() == str(cb).strip().upper():
        return 100.0
    da, db = str(_v(a, "description") or ""), str(_v(b, "description") or "")
    # vendors write the same item as "M.S. HEX BOLT M12 X 50" and "MS Hex Bolt M12x50"
    squash = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())  # noqa: E731
    words = lambda t: re.sub(r"[^a-z0-9 ]", "", t.lower().replace(".", ""))  # noqa: E731
    s = max(fuzz.token_set_ratio(words(da), words(db)), fuzz.ratio(squash(da), squash(db)))
    ha, hb = _v(a, "hsn_sac"), _v(b, "hsn_sac")
    if ha and hb and str(ha)[:4] == str(hb)[:4]:
        s += 10
    return s


def _match(po_lines: list[dict], other: list[dict], min_score: float = 70) -> dict[int, int]:
    """other index -> po index, greedy on best similarity."""
    pairs = sorted(((_line_key_score(p, o), pi, oi) for pi, p in enumerate(po_lines) for oi, o in enumerate(other)),
                   reverse=True)
    used_p, used_o, out = set(), set(), {}
    for score, pi, oi in pairs:
        if score < min_score or pi in used_p or oi in used_o:
            continue
        used_p.add(pi)
        used_o.add(oi)
        out[oi] = pi
    return out


def _same_ref(a, b) -> bool:
    norm = lambda x: "".join(ch for ch in str(x or "").upper() if ch.isalnum()).lstrip("0")  # noqa: E731
    return bool(a) and bool(b) and (norm(a) == norm(b) or norm(a).endswith(norm(b)) or norm(b).endswith(norm(a)))


def three_way_match(po: ExtractedDoc, grns: list[ExtractedDoc], inv: ExtractedDoc, tol: Tolerances | None = None) -> MatchResult:
    tol = tol or Tolerances()
    issues: list[Issue] = []
    po_no = po.v("po_number")
    if inv.v("po_reference") and not _same_ref(inv.v("po_reference"), po_no):
        issues.append(Issue("reference", "block", f"Invoice refers to PO {inv.v('po_reference')}, not {po_no}"))
    for g in grns:
        if g.v("po_reference") and not _same_ref(g.v("po_reference"), po_no):
            issues.append(Issue("reference", "block", f"GRN {g.v('grn_number')} refers to PO {g.v('po_reference')}, not {po_no}"))
        if g.v("supplier_invoice_reference") and inv.v("invoice_number") and not _same_ref(g.v("supplier_invoice_reference"), inv.v("invoice_number")):
            issues.append(Issue("reference", "warn", f"GRN {g.v('grn_number')} records supplier invoice {g.v('supplier_invoice_reference')}, "
                                                     f"this invoice is {inv.v('invoice_number')}"))
    sup = {x for x in (po.v("supplier_gstin"), inv.v("supplier_gstin"), *(g.v("supplier_gstin") for g in grns)) if x}
    if len(sup) > 1:
        issues.append(Issue("supplier", "block", "Supplier GSTIN differs between PO, GRN and invoice"))

    # aggregate GRN quantities onto PO lines
    received = [0.0] * len(po.lines)
    accepted = [0.0] * len(po.lines)
    has_accept = [False] * len(po.lines)
    for g in grns:
        m = _match(po.lines, g.lines)
        for oi, gl in enumerate(g.lines):
            if oi not in m:
                issues.append(Issue("extra_line", "warn", f"GRN {g.v('grn_number')} line '{_v(gl, 'description')}' isn't on the PO"))
                continue
            pi = m[oi]
            rec = _v(gl, "quantity_received") or 0.0
            acc = _v(gl, "quantity_accepted")
            received[pi] += rec
            if acc is not None:
                accepted[pi] += acc
                has_accept[pi] = True
            else:
                accepted[pi] += rec

    inv_map = _match(po.lines, inv.lines)
    billed_po_lines = set(inv_map.values())
    rows = []
    expected_taxable = 0.0
    for oi, il in enumerate(inv.lines):
        desc = _v(il, "description")
        if oi not in inv_map:
            issues.append(Issue("extra_line", "block", f"Invoice line '{desc}' is not on the PO", oi))
            rows.append({"invoice_line": oi, "po_line": None, "description": desc, "status": "not_on_po"})
            continue
        pi = inv_map[oi]
        pl = po.lines[pi]
        q_inv, r_inv = _v(il, "quantity"), _v(il, "rate")
        q_po, r_po = _v(pl, "quantity"), _v(pl, "rate")
        row = {"invoice_line": oi, "po_line": pi, "description": desc, "qty_ordered": q_po,
               "qty_received": received[pi] if grns else None, "qty_accepted": accepted[pi] if grns else None,
               "qty_invoiced": q_inv, "rate_po": r_po, "rate_invoice": r_inv,
               "gst_po": _v(pl, "gst_rate"), "gst_invoice": _v(il, "gst_rate"), "problems": []}
        line_no = oi + 1
        if r_inv is not None and r_po is not None and r_inv > r_po * (1 + tol.price_pct / 100) + 1e-9:
            diff = (r_inv - r_po) * (q_inv or 0)
            msg = f"Line {line_no}: billed at ₹{r_inv:,.2f} vs PO rate ₹{r_po:,.2f} (₹{diff:,.2f} over)"
            issues.append(Issue("price", "block", msg, oi))
            row["problems"].append(msg)
        if grns and q_inv is not None:
            limit = accepted[pi] * (1 + tol.qty_pct / 100)
            if q_inv > limit + 1e-9:
                msg = f"Line {line_no}: billed {q_inv:g} but only {accepted[pi]:g} {'accepted' if has_accept[pi] else 'received'}"
                issues.append(Issue("quantity", "block", msg, oi))
                row["problems"].append(msg)
        if q_inv is not None and q_po is not None and q_inv > q_po + 1e-9:
            msg = f"Line {line_no}: billed {q_inv:g} exceeds ordered {q_po:g}"
            issues.append(Issue("quantity", "block", msg, oi))
            row["problems"].append(msg)
        g_po, g_inv = row["gst_po"], row["gst_invoice"]
        if g_po is not None and g_inv is not None and abs(g_po - g_inv) > 0.001:
            msg = f"Line {line_no}: GST {g_inv:g}% on invoice vs {g_po:g}% on PO"
            issues.append(Issue("tax", "block", msg, oi))
            row["problems"].append(msg)
        if r_po is not None and q_inv is not None:
            payable_qty = min(q_inv, accepted[pi]) if grns else q_inv
            expected_taxable += payable_qty * r_po - (_v(il, "discount") or 0)
        row["status"] = "mismatch" if row["problems"] else "ok"
        rows.append(row)
    for pi, pl in enumerate(po.lines):
        if pi not in billed_po_lines and grns and accepted[pi] > 0:
            issues.append(Issue("missing_line", "warn", f"PO line '{_v(pl, 'description')}' was received but not billed"))

    inv_taxable = inv.v("subtotal")
    if inv_taxable is not None and inv.lines and all(r.get("po_line") is not None for r in rows):
        if abs(inv_taxable - expected_taxable) > max(tol.amount_abs, expected_taxable * 0.001):
            issues.append(Issue("total", "block", f"Invoice taxable value ₹{inv_taxable:,.2f} vs ₹{expected_taxable:,.2f} "
                                                  f"payable at PO rates for accepted quantities "
                                                  f"(₹{inv_taxable - expected_taxable:,.2f} difference)"))

    incomplete = not po.lines or not inv.lines
    blocks = [i for i in issues if i.severity == "block"]
    if incomplete:
        status, summary = "incomplete", "Can't match yet — line items are missing on the PO or invoice"
    elif blocks:
        status = "mismatch"
        summary = f"{len(blocks)} mismatch{'es' if len(blocks) > 1 else ''} — hold payment until resolved"
    else:
        status = "matched"
        summary = "PO, goods received and invoice agree" + (" (see notes)" if issues else "")
    return MatchResult(status=status, summary=summary, lines=rows, issues=issues)
