"""Field-level confidence built from evidence, never from the model's self-reported certainty.

Signals: (a) self-consistency between independent passes, (b) arithmetic / format / compliance
checks, (c) pre-extraction document quality, (d) historical accuracy for this vendor x doc type x
field, plus text-layer grounding for digital PDFs and raw/normalised consistency.

score = sigmoid(bias + sum(w_i * x_i)), then the whole document is multiplied by a penalty if any
error-severity check failed. Weights start from engineering judgement (below) and are refit
weekly against reviewer corrections by eval/backtest.py; features are persisted per field so the
refit uses exactly what the scorer saw. Hard rules force review regardless of score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from sereno.extraction.result import ExtractedDoc, FieldValue
from sereno.validation.checks import CheckResult

DEFAULT_WEIGHTS: dict[str, float] = {
    "bias": 2.0,
    "legibility_clear": 0.5,      # model self-report: weak on its own, never sufficient alone
    "absent_optional": 0.5,       # optional field reported as not on the document
    "legibility_partly": -2.0,
    "legibility_illegible": -5.0,
    "agree": 2.0,
    "disagree": -3.5,
    "verifier_certain": 0.8,
    "raw_inconsistent": -2.5,
    "raw_consistent": 0.3,
    "in_text_layer": 2.0,
    "not_in_text_layer": -2.0,
    "format_ok": 1.0,
    "format_bad": -4.0,
    "checks_passed": 0.8,
    "checks_failed": -4.0,
    "doc_check_failed": 0.0,      # logged for the backtest; the multiplicative doc penalty already applies
    "quality_digital": 1.0,
    "quality_poor": -1.0,
    "handwriting": -0.7,
    "indic_script": -0.5,
    "history": 1.0,
    "unparseable": -4.0,
    "missing_required": -6.0,
    # independent machine evidence (added with QR decoding, crop re-reads, master data)
    "code_match": 4.0,            # agrees with a machine-readable code (signed e-invoice QR, UPI QR, barcode)
    "code_filled": 3.0,           # absent/illegible in print, taken from the code
    "code_mismatch": -4.0,
    "votes3": 1.5,                # three independent readings (page, page strips, zoomed crop) agree
    "vote_split": -2.0,
    "repaired": -3.0,             # value corrected by arithmetic + another reading: always shown to a person
    "master_match": 1.5,          # matches the company's own records / this vendor's history
}


@dataclass
class FieldScore:
    score: float
    band: str
    needs_review: bool
    reason: str | None
    reason_codes: list[str]
    features: dict[str, float]


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def history_signal(correct: int, total: int, prior_acc: float = 0.9, prior_n: float = 10.0) -> float:
    """Shrunk log-odds shift: 0 with no history, grows as evidence accumulates."""
    if total <= 0:
        return 0.0
    post = (correct + prior_acc * prior_n) / (total + prior_n)
    return _logit(post) - _logit(prior_acc)


def features_for(fv: FieldValue, checks_by_key: dict[str, list[CheckResult]], doc_failed: bool,
                 quality_bucket: str, doc: ExtractedDoc, hist: float) -> dict[str, float]:
    x: dict[str, float] = {"bias": 1.0}
    x["legibility_clear"] = float(fv.legibility == "clear" and fv.value is not None)
    x["absent_optional"] = float(fv.value is None and fv.legibility == "not_present" and not fv.spec.required)
    x["legibility_partly"] = float(fv.legibility == "partly_legible")
    x["legibility_illegible"] = float(fv.legibility == "illegible")
    x["agree"] = float(fv.double_read and fv.agreement is True)
    x["disagree"] = float(fv.double_read and fv.agreement is False)
    x["verifier_certain"] = float(bool(fv.verifier_certain) and fv.verifier_choice in ("A", "B", "neither"))
    x["raw_inconsistent"] = float(fv.raw_consistent is False)
    x["raw_consistent"] = float(fv.raw_consistent is True)
    x["in_text_layer"] = float(fv.in_text_layer is True)
    x["not_in_text_layer"] = float(fv.in_text_layer is False)
    x["format_ok"] = float(fv.format_ok is True)
    x["format_bad"] = float(fv.format_ok is False)
    cs = checks_by_key.get(fv.key, [])
    x["checks_passed"] = float(min(2, sum(1 for c in cs if c.status == "pass")))
    x["checks_failed"] = float(min(2, sum(1 for c in cs if c.failed and c.severity == "error"
                                          and not c.check_id.startswith("format:"))))
    x["doc_check_failed"] = float(doc_failed)
    x["quality_digital"] = float(quality_bucket == "digital")
    x["quality_poor"] = float(quality_bucket == "poor_scan")
    x["handwriting"] = float(doc.handwriting)
    x["indic_script"] = float(any(l not in ("english", "other") for l in doc.languages))
    x["history"] = hist
    x["unparseable"] = float(fv.unparseable)
    x["missing_required"] = float(fv.spec.required and fv.value is None and fv.line_index is None)
    x["code_match"] = float(fv.code_agrees is True and not fv.code_filled)
    x["code_filled"] = float(fv.code_filled)
    x["code_mismatch"] = float(fv.code_agrees is False)
    x["votes3"] = float(fv.votes == "3/3")
    x["vote_split"] = float(fv.votes in ("2/3", "1/1/1", "illegible on zoom"))
    x["repaired"] = float(fv.repaired)
    x["master_match"] = float(fv.master_match is True)
    return x


def score_field(x: dict[str, float], weights: dict[str, float]) -> float:
    return _sigmoid(sum(weights.get(k, 0.0) * v for k, v in x.items()))


def score_document(doc: ExtractedDoc, checks: list[CheckResult], quality_bucket: str,
                   history: dict[str, tuple[int, int]], auto_accept: float, high_stakes: float,
                   doc_penalty: float, weights: dict[str, float] | None = None) -> dict[str, FieldScore]:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    checks_by_key: dict[str, list[CheckResult]] = {}
    for c in checks:
        for k in c.field_keys:
            checks_by_key.setdefault(k, []).append(c)
    doc_failed = any(c.failed and c.severity == "error" for c in checks)
    out: dict[str, FieldScore] = {}
    for fv in doc.all_fields():
        hname = f"line.{fv.spec.name}" if fv.line_index is not None else fv.spec.name
        c_ok, c_tot = history.get(hname, (0, 0))
        x = features_for(fv, checks_by_key, doc_failed, quality_bucket, doc, history_signal(c_ok, c_tot))
        raw = score_field(x, w)
        # Whole-document penalty: every score (and band) drops when any arithmetic/compliance check
        # fails. Routing still uses the unpenalised score for fields NOT implicated in a failed check,
        # so the reviewer is pointed at the fields that can explain the mismatch instead of re-keying
        # the whole document (the document is in review regardless).
        s = raw * doc_penalty if doc_failed else raw
        threshold = high_stakes if fv.spec.high_stakes else auto_accept
        reasons: list[tuple[int, str, str]] = []  # (priority, code, plain text)
        failed_here = [c for c in checks_by_key.get(fv.key, []) if c.failed]
        for c in failed_here:
            if c.severity == "error":
                # the most specific explanation first: signed QR, own records, FY swap, duplicate, bank change
                specific = c.check_id.split(":")[0] in ("qr", "own_gstin", "fy_date", "duplicate", "vendor_bank_changed")
                reasons.append((-1 if specific else 0, f"check:{c.check_id}", c.message))
            else:
                reasons.append((3, f"warn:{c.check_id}", c.message))
        if x["missing_required"]:
            reasons.append((1, "missing_required", f"{fv.spec.label} wasn't found on the document"))
        if fv.repaired:
            reasons.append((0, "repaired", fv.suggestion_reason or "Corrected automatically; please confirm"))
        elif x["disagree"]:
            a, b = fv.value, fv.alt_value
            why = f"Two independent readings differ ({_fmt(a)} vs {_fmt(b)})"
            if fv.votes == "2/3":
                why = f"2 of 3 independent readings say {_fmt(a)} (one said {_fmt(b)})"
            elif fv.votes == "1/1/1":
                why = f"Three readings disagree ({_fmt(a)}, {_fmt(b)}, {_fmt(fv.third_value)})"
            reasons.append((1, "disagree", why))
        if fv.suggested_value is not None and not fv.repaired:
            reasons.append((1, "suggestion", f"Suggested {_fmt(fv.suggested_value)}: {fv.suggestion_reason}"))
        if fv.votes == "illegible on zoom":
            reasons.append((1, "illegible_zoom", f"{fv.spec.label} could not be read even when zoomed in"))
        if x["unparseable"]:
            reasons.append((1, "unparseable", f"{fv.spec.label} couldn't be read as a {fv.spec.type}"))
        if fv.legibility == "illegible":
            reasons.append((1, "illegible", f"{fv.spec.label} is not legible on the document"))
        elif fv.legibility == "partly_legible" and fv.value is not None:
            reasons.append((2, "partly_legible", f"{fv.spec.label} is partly unclear on the document"))
        if x["raw_inconsistent"]:
            reasons.append((2, "raw_inconsistent", "The value doesn't match the text as printed"))
        if x["not_in_text_layer"] and fv.spec.high_stakes:
            reasons.append((2, "not_in_text_layer", "Value not found in the PDF's text"))
        hard = any(p <= 1 for p, _, _ in reasons) or (fv.spec.high_stakes and fv.legibility == "partly_legible"
                                                     and fv.value is not None)
        needs_review = hard or raw < threshold
        if needs_review and not reasons:
            reasons.append((4, "low_score", "Low confidence reading; please confirm"))
        band = "high" if s >= threshold and not needs_review else ("medium" if s >= 0.6 and not hard else "low")
        if doc_failed and band == "high":
            band = "medium"
        reasons.sort(key=lambda r: r[0])
        out[fv.key] = FieldScore(score=round(s, 4), band=band, needs_review=needs_review,
                                 reason=reasons[0][2] if needs_review and reasons else None,
                                 reason_codes=[r[1] for r in reasons], features=x)
    return out


def _fmt(v) -> str:
    if v is None:
        return "blank"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)
