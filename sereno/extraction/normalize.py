"""Deterministic parsing of what the model read. The model returns strings; this module owns the
Indian-specific conventions (lakh grouping, Devanagari numerals, day-first dates, Rs./- suffixes)
so they are tested code, not prompt behaviour. A value whose `value` and `raw_text` parse to
different things is itself a confidence signal."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Every Indic digit block we meet on Indian business documents -> ASCII 0-9.
_INDIC_ZEROS = {
    0x0966: "Devanagari (Hindi, Marathi, Nepali)", 0x09E6: "Bengali/Assamese", 0x0A66: "Gurmukhi (Punjabi)",
    0x0AE6: "Gujarati", 0x0B66: "Odia", 0x0BE6: "Tamil", 0x0C66: "Telugu", 0x0CE6: "Kannada", 0x0D66: "Malayalam",
}
_DIGITS = str.maketrans({chr(z + i): str(i) for z in _INDIC_ZEROS for i in range(10)})


def fold_digits(s: str) -> str:
    return s.translate(_DIGITS)


_CURRENCY = re.compile(r"(₹|rs\.?|inr|rupees?|/-|\bonly\b)", re.I)


def parse_amount(s: str | None) -> Decimal | None:
    if s is None:
        return None
    t = fold_digits(str(s)).strip()
    if not t:
        return None
    if t.lower() in ("nil", "-", "--", "—"):
        return Decimal("0")
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    t = _CURRENCY.sub("", t).strip()
    if t.endswith("-") and not t.endswith("/-"):
        t = t[:-1]
    if t.startswith("-") or t.startswith("−"):
        neg, t = True, t[1:]
    elif t.startswith("+"):
        t = t[1:]
    t = t.replace(",", "").replace(" ", "").replace(" ", "")
    # "1.234,50" European style is not used in India; a lone comma decimal is treated as an error
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", t):
        return None
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    return -v if neg else v


def parse_int(s: str | None) -> int | None:
    v = parse_amount(s)
    if v is None or v != v.to_integral_value():
        return None
    return int(v)


def parse_percent(s: str | None) -> Decimal | None:
    if s is None or not str(s).strip():
        return None
    t = fold_digits(str(s)).replace("%", " ").strip()
    parts = [p for p in re.split(r"\s*\+\s*", t) if p]
    total = Decimal("0")
    for p in parts:
        v = parse_amount(p)
        if v is None:
            return None
        total += v
    return total


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
# Hindi and Marathi month names (with common spelling variants), normalised without nukta/chandrabindu
_INDIC_MONTHS = {
    "जनवरी": 1, "जानेवारी": 1, "फरवरी": 2, "फेब्रुवारी": 2, "मार्च": 3, "अप्रैल": 4, "अप्रेल": 4, "एप्रिल": 4,
    "मई": 5, "मे": 5, "जून": 6, "जुलाई": 7, "जुलै": 7, "अगस्त": 8, "ऑगस्ट": 8, "अगस्ट": 8,
    "सितंबर": 9, "सितम्बर": 9, "सप्टेंबर": 9, "अक्टूबर": 10, "अक्तूबर": 10, "ऑक्टोबर": 10,
    "नवंबर": 11, "नवम्बर": 11, "नोव्हेंबर": 11, "दिसंबर": 12, "दिसम्बर": 12, "डिसेंबर": 12,
}


def devanagari_key(t: str) -> str:
    """Spelling-tolerant key: drop nukta, fold chandrabindu into anusvara."""
    return t.replace("\u093c", "").replace("\u0901", "\u0902").strip()


_INDIC_MONTHS = {devanagari_key(k): v for k, v in _INDIC_MONTHS.items()}


def parse_date(s: str | None) -> date | None:
    """ISO first (what we ask the model for), then Indian day-first conventions."""
    if s is None:
        return None
    t = fold_digits(str(s)).strip().lower().replace(",", " ")
    if not t:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return _mk(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{2,4})", t)
    if m:  # day-first (Indian convention)
        return _mk(_year(m[3]), int(m[2]), int(m[1]))
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s*[\-/ .]?\s*([a-z]{3,9})\.?\s*[\-/ .]?\s*(\d{2,4})", t)
    if m and m[2][:3] in _MONTHS:
        return _mk(_year(m[3]), _MONTHS[m[2][:3]], int(m[1]))
    m = re.fullmatch(r"([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})", t)
    if m and m[1][:3] in _MONTHS:
        return _mk(int(m[3]), _MONTHS[m[1][:3]], int(m[2]))
    m = re.fullmatch(r"(\d{1,2})\s*[\-/ .,]?\s*([\u0900-\u097f]+)\s*[\-/ .,]?\s*(\d{2,4})", t)
    if m and devanagari_key(m[2]) in _INDIC_MONTHS:  # "5 जून 2025", "05-जुलै-25"
        return _mk(_year(m[3]), _INDIC_MONTHS[devanagari_key(m[2])], int(m[1]))
    return None


def _year(y: str) -> int:
    v = int(y)
    return v + 2000 if v < 100 else v


def _mk(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def clean_code(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", fold_digits(str(s or "")).upper())


VEHICLE_RE = re.compile(r"^([A-Z]{2}\d{1,2}[A-Z]{0,3}\d{1,4}|\d{2}BH\d{4}[A-Z]{1,2})$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
HSN_RE = re.compile(r"^\d{4}(\d{2}){0,2}$")


def normalise(ftype: str, value: str | None):
    """Typed value for a spec type, or None if absent/unparseable."""
    if value is None or str(value).strip() == "":
        return None
    if ftype == "number":
        v = parse_amount(value)
        return float(v) if v is not None else None
    if ftype == "percent":
        v = parse_percent(value)
        return float(v) if v is not None else None
    if ftype == "integer":
        return parse_int(value)
    if ftype == "date":
        d = parse_date(value)
        return d.isoformat() if d else None
    if ftype in ("gstin", "pan", "ifsc", "vehicle", "hsn"):
        return clean_code(value) or None
    return re.sub(r"\s+", " ", str(value)).strip() or None


def comparable(ftype: str, v) -> object:
    if v is None:
        return None
    if ftype in ("number", "percent"):
        return round(float(v), 2)
    if ftype in ("string", "text", "state"):
        return re.sub(r"[^a-z0-9ऀ-ॿ]", "", str(v).lower())
    return v


def values_agree(ftype: str, a, b) -> bool:
    ca, cb = comparable(ftype, a), comparable(ftype, b)
    if ca is None or cb is None:
        return ca is None and cb is None
    if ftype in ("number", "percent"):
        return abs(float(ca) - float(cb)) < 0.005
    if ftype == "text" and len(ca) > 25:
        # long free text (addresses, descriptions, clause summaries): tolerate wording noise
        from rapidfuzz import fuzz
        return fuzz.ratio(ca, cb) >= 92
    return ca == cb


def raw_consistent(ftype: str, value, raw_text: str | None) -> bool | None:
    """Does the verbatim transcription support the normalised value? None when not checkable."""
    if value is None or not raw_text or not str(raw_text).strip():
        return None
    if ftype in ("number", "percent", "integer"):
        rv = parse_percent(raw_text) if ftype == "percent" else parse_amount(raw_text)
        if rv is None:
            m = re.search(r"\d[\d,]*(?:\.\d+)?", fold_digits(raw_text))
            rv = parse_amount(m.group(0)) if m else None
        if rv is None:
            return None
        return abs(float(rv) - float(value)) < 0.005 or abs(abs(float(rv)) - abs(float(value))) < 0.005
    if ftype == "date":
        d = parse_date(raw_text)
        return None if d is None else d.isoformat() == value
    if ftype in ("gstin", "pan", "ifsc", "vehicle", "hsn"):
        return clean_code(raw_text) == value
    return None


def today() -> date:
    return datetime.now().date()


def shape_mask(s: str | None) -> str:
    """Shape of a value without its content: INV/24-25/0123 -> AAA/99-99/9999."""
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", fold_digits(str(s or ""))))[:40]
