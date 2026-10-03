"""Which extracted values are already proven by something other than the model?

A field proven here needs no second model reading. Sources, strongest first:
* the signed e-invoice QR / a barcode carries the same value,
* an exact arithmetic identity holds with the value in it (qty x rate = amount, lines sum to the
  taxable value, taxable + GST = total, amount in words = figures),
* the PDF's own text layer contains it (long enough not to match by coincidence),
* the company's records hold it (own GSTIN, a vendor seen on reviewed documents).

Everything else is re-read once from a zoomed crop by a different model (crossread.py). Research
basis: cheap deterministic verifiers before extra model calls (FrugalGPT-style cascades), and two
structurally different readings as the disagreement signal (docs/ARCHITECTURE.md).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sereno.extraction.result import ExtractedDoc, FieldValue
from sereno.validation.checks import CheckResult

# Identities tight enough that a misread value would break them (tax-rate checks allow rounding
# of 0.1%, so they confirm consistency but don't prove each digit).
EXACT = ("line_math", "line_total", "lines_sum_subtotal", "tax_sum", "grand_total", "amount_in_words", "total_freight")
# Tax-rate identities allow 0.1% rounding on amounts, but a misread rate (18 for 28) can't pass.
RATE = ("line_tax", "tax_rate_reconcile")


def _num(x) -> Decimal | None:
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _exact(c: CheckResult) -> bool:
    d = c.details or {}
    pairs = [("calc", "printed"), ("lines_sum", "subtotal")]
    for a, b in pairs:
        if a in d and b in d:
            x, y = _num(d[a]), _num(d[b])
            return x is not None and y is not None and abs(x - y) <= Decimal("0.011")
    return False


def _strong_text(fv: FieldValue) -> bool:
    """A short number ("2", "18") is in any invoice's text by coincidence; it proves nothing."""
    if fv.spec.is_numeric:
        return sum(ch.isdigit() for ch in (fv.raw_text or str(fv.value))) >= 4
    return True


def mark_verified(doc: ExtractedDoc, checks: list[CheckResult]) -> None:
    by_key = doc.by_key()
    failing = {k for c in checks if c.failed and c.severity == "error" for k in c.field_keys}
    for fv in by_key.values():
        fv.verified_by = []
    for c in checks:
        kind = c.check_id.split(":")[0]
        if c.status != "pass" or not ((kind in EXACT and _exact(c)) or kind in RATE):
            continue
        for k in c.field_keys:
            fv = by_key.get(k)
            if fv is None or k in failing or "arithmetic" in fv.verified_by or (kind in RATE and fv.spec.type != "percent"):
                continue
            if fv.value is None and (kind in RATE or fv.legibility != "not_present"):
                continue
            # an exact identity also proves an absent term (no hidden discount, cess or charge)
            fv.verified_by.append("arithmetic")
    for fv in by_key.values():
        if fv.value is None:
            continue
        if fv.code_agrees is True:
            fv.verified_by.append("code")
        if fv.in_text_layer is True and _strong_text(fv):
            fv.verified_by.append("text_layer")
        if fv.master_match is True:
            fv.verified_by.append("records")


_TAG = {"arithmetic": "adds up", "text_layer": "in the PDF text", "records": "matches your records"}


def tags(fv: FieldValue) -> list[str]:
    """Plain tags for the reviewer (codes already tag themselves in codes.py)."""
    return [_TAG[v] for v in fv.verified_by if v in _TAG]
