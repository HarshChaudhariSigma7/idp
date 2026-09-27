"""Plain-language status for finance/ops users. No percentages, no jargon."""
from __future__ import annotations

from sereno.validation.checks import CheckResult

# Short phrases for the headline, keyed by check id prefix. Order = priority.
_CHECK_PHRASES = [
    ("grand_total", "invoice total doesn't add up"),
    ("tax_type_vs_state", "tax type doesn't match the states (IGST vs CGST+SGST)"),
    ("tax_rate_reconcile", "tax amount doesn't match the GST rate"),
    ("tax_sum", "tax amount doesn't match the line items"),
    ("cgst_eq_sgst", "CGST and SGST don't match"),
    ("lines_sum_subtotal", "line items don't add up to the taxable value"),
    ("line_tax", "a line's GST doesn't match its rate"),
    ("line_math", "quantity × rate doesn't match a line amount"),
    ("total_freight", "freight total doesn't add up"),
    ("freight_math", "freight doesn't match weight × rate"),
    ("amount_in_words", "amount in words doesn't match the total"),
    ("round_off", "round-off is too large"),
    ("format:", "a GSTIN / code looks misread"),
    ("gst_slab", "an unusual GST rate"),
    ("accept_reject", "accepted + rejected quantity doesn't equal received"),
    ("date:", "a date looks wrong"),
    ("required_fields", "some required details are missing"),
]

STATUS_LABELS = {
    "uploaded": "Queued",
    "processing": "Reading document…",
    "needs_review": "Needs review",
    "ready": "Ready to export",
    "unreadable": "Couldn't read",
    "failed": "Couldn't process",
    "exported": "Exported",
}


def review_headline(failed_checks: list[CheckResult], flagged_labels: list[str]) -> str:
    for prefix, phrase in _CHECK_PHRASES:
        if any(c.check_id.startswith(prefix) for c in failed_checks):
            extra = len(flagged_labels) - 1
            return f"Needs your review — {phrase}" + (f" (+{extra} more)" if extra > 0 else "")
    if len(flagged_labels) == 1:
        return f"Needs your review — please confirm {flagged_labels[0]}"
    if flagged_labels:
        return f"Needs your review — {len(flagged_labels)} fields to confirm ({', '.join(flagged_labels[:2])}…)"
    return "Needs your review"


def ready_message(total_fields: int) -> str:
    return "Ready to export — all figures checked and consistent"
