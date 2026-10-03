"""Arithmetic-guided repair.

When the figures on a reading don't reconcile, the equations themselves say which number is wrong
and what it should be. We search a small, plausible space: values another reading actually saw
(pass B, the zoomed crop read, a QR code) and single scanner/handwriting slips on the printed
digits (look-alike digits such as 3/8, 1/7, 5/6; a dropped or doubled digit; swapped neighbours;
a shifted decimal point). A candidate counts only if it makes EVERY failing arithmetic check pass
without breaking another one, and only a unique solution is used.

* Backed by another reading (evidence) -> the value is corrected, marked as repaired, and still
  shown to a person with the reason (one keystroke to confirm).
* Pure slip, no reading saw it -> offered as a suggestion only; the printed reading is kept.
* No solution and a zoomed re-read confirms the print -> the document itself doesn't add up.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from sereno.extraction.normalize import values_agree
from sereno.extraction.result import ExtractedDoc, FieldValue
from sereno.validation.checks import CheckResult, run_checks

ARITHMETIC = ("line_math", "line_tax", "line_total", "lines_sum_subtotal", "tax_sum", "tax_rate_reconcile",
              "cgst_eq_sgst", "grand_total", "round_off", "amount_in_words", "total_freight", "accept_reject")
DIGIT_SLIPS = {"0": "869", "1": "74", "2": "73", "3": "859", "4": "91", "5": "638", "6": "5809", "7": "12",
               "8": "30695", "9": "4807"}
MAX_FIELDS = 14


@dataclass
class Repair:
    key: str
    value: float
    old: object
    source: str          # "pass B" | "zoomed re-read" | "QR code" | "look-alike digits" ...
    evidence_backed: bool


def failing_arithmetic(checks: list[CheckResult]) -> list[CheckResult]:
    return [c for c in checks if c.failed and c.severity == "error" and c.check_id.split(":")[0] in ARITHMETIC]


def implicated_numeric(doc: ExtractedDoc, checks: list[CheckResult]) -> list[FieldValue]:
    keys = []
    for c in failing_arithmetic(checks):
        keys += c.field_keys
    by_key = doc.by_key()
    out, seen = [], set()
    for k in keys:
        fv = by_key.get(k)
        if fv is not None and fv.spec.is_numeric and fv.value is not None and k not in seen:
            seen.add(k)
            out.append(fv)
    return out[:MAX_FIELDS]


def _printed_digits(value: float, raw: str | None) -> tuple[str, str]:
    """(whole, fraction) digits as printed: '501' stays a whole number, '1,23,456.50' keeps 2 decimals."""
    import re
    from sereno.extraction.normalize import fold_digits
    m = re.search(r"\d[\d,]*(?:\.(\d+))?", fold_digits(raw or ""))
    if m:
        num = m.group(0).replace(",", "")
        try:
            if abs(float(num) - abs(value)) < 0.005:
                whole, _, frac = num.partition(".")
                return whole, frac
        except ValueError:
            pass
    if float(value).is_integer():
        return str(int(abs(value))), ""
    whole, frac = f"{abs(value):.2f}".split(".")
    return whole, frac


def slips(value: float, raw: str | None = None) -> list[tuple[float, str]]:
    """Plausible misreads of a printed number, as (value, description)."""
    whole, frac = _printed_digits(value, raw)
    digits = whole + frac  # work on all digits, re-insert the decimal point after
    n_whole = len(whole)
    out: dict[float, str] = {}

    def add(ds: str, nw: int, why: str):
        if not ds or nw < 1:
            return
        try:
            v = float(ds[:nw] + ("." + ds[nw:] if ds[nw:] else ""))
        except ValueError:
            return
        v = -v if value < 0 else v
        if abs(v - value) > 0.004:
            out.setdefault(round(v, 2), why)

    for i, d in enumerate(digits):
        for alt in DIGIT_SLIPS.get(d, ""):
            add(digits[:i] + alt + digits[i + 1:], n_whole, f"a {alt} read as a {d}")
        if i < n_whole:
            add(digits[:i] + digits[i + 1:], n_whole - 1, "a dropped digit")
            add(digits[:i] + d + digits[i:], n_whole + 1, "a doubled digit")
        if i + 1 < len(digits) and digits[i] != digits[i + 1]:
            add(digits[:i] + digits[i + 1] + digits[i] + digits[i + 2:], n_whole, "two digits swapped")
    for f, why in ((10, "a shifted decimal point"), (0.1, "a shifted decimal point"), (100, "a missing decimal point"),
                   (0.01, "a missing decimal point")):
        if frac or f >= 1:  # a whole number can't have had its decimal point dropped
            out.setdefault(round(value * f, 2), why)
    out.pop(round(value, 2), None)
    return list(out.items())


def _arith_ids(checks: list[CheckResult]) -> set[str]:
    return {c.check_id for c in failing_arithmetic(checks)}


def _error_ids(checks: list[CheckResult]) -> set[str]:
    return {c.check_id for c in checks if c.failed and c.severity == "error"}


def search(doc: ExtractedDoc, checks: list[CheckResult], evidence: dict[str, list[tuple[float, str]]],
           abs_tol: float, rel_tol: float) -> tuple[list[Repair], Repair | None]:
    """Returns (evidence-backed repairs to apply, slip-only suggestion). Unique solutions only."""
    base_errors = _error_ids(checks)
    fields = implicated_numeric(doc, checks)

    def resolves(assign: list[tuple[FieldValue, float]]) -> bool:
        old = [(fv, fv.value) for fv, _ in assign]
        for fv, v in assign:
            fv.value = v
        try:
            new = run_checks(doc, abs_tol, rel_tol)
        finally:
            for fv, v in old:
                fv.value = v
        return not _arith_ids(new) and not (_error_ids(new) - base_errors)

    backed: list[Repair] = []
    slip_only: list[Repair] = []
    for fv in fields:
        tried = set()
        for v, src in evidence.get(fv.key, []):
            if v is None or values_agree(fv.spec.type, v, fv.value) or round(v, 2) in tried:
                continue
            tried.add(round(v, 2))
            if resolves([(fv, v)]):
                backed.append(Repair(fv.key, v, fv.value, src, True))
        for v, why in slips(float(fv.value), fv.raw_text):
            if round(v, 2) in tried:
                continue
            tried.add(round(v, 2))
            if resolves([(fv, v)]):
                slip_only.append(Repair(fv.key, v, fv.value, why, False))
    if len({(r.key, round(r.value, 2)) for r in backed}) == 1:
        return [backed[0]], None
    if not backed:
        # two independent misreads, each seen by another reading
        options = [(fv, v, src) for fv in fields for v, src in evidence.get(fv.key, [])
                   if v is not None and not values_agree(fv.spec.type, v, fv.value)]
        solutions = []
        for (f1, v1, s1), (f2, v2, s2) in combinations(options, 2):
            if f1.key != f2.key and resolves([(f1, v1), (f2, v2)]):
                solutions.append([Repair(f1.key, v1, f1.value, s1, True), Repair(f2.key, v2, f2.value, s2, True)])
        if len(solutions) == 1:
            return solutions[0], None
    uniq = {(r.key, round(r.value, 2)): r for r in slip_only}
    return [], (next(iter(uniq.values())) if len(uniq) == 1 else None)


def apply(doc: ExtractedDoc, repairs: list[Repair], suggestion: Repair | None) -> None:
    by_key = doc.by_key()
    for r in repairs:
        fv = by_key[r.key]
        fv.alt_value, fv.value = fv.value, r.value
        fv.repaired = True
        fv.agreement = False
        fv.suggestion_reason = f"Corrected from {_fmt(r.old)} to {_fmt(r.value)}: the {r.source} agrees and the totals add up"
        fv.evidence.append("totals reconcile after correction")
    if suggestion is not None:
        fv = by_key[suggestion.key]
        fv.suggested_value = suggestion.value
        fv.suggestion_reason = (f"{_fmt(suggestion.value)} would make every total add up "
                                f"(the print may contain {suggestion.source})")


def _fmt(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"
    return str(v)
