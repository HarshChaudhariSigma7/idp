"""India-specific validators: GSTIN checksum, state codes, GST slabs, amount-in-words."""
from __future__ import annotations

import re
from decimal import Decimal

_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman and Diu", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "28": "Andhra Pradesh (old)", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory", "99": "Centre Jurisdiction",
}
_STATE_ALIASES = {"orissa": "21", "pondicherry": "34", "new delhi": "07", "nct of delhi": "07",
                  "j&k": "01", "up": "09", "mp": "23", "tn": "33", "ap": "37", "wb": "19"}

# Old slabs plus the rationalised 2025 structure (5 / 18 / 40); 0.1 and 0.25/1.5/3 are special rates.
GST_RATES = {Decimal(x) for x in ("0", "0.1", "0.25", "1", "1.5", "3", "5", "6", "7.5", "12", "18", "28", "40")}


def gstin_check_char(first14: str) -> str:
    factor, total = 2, 0
    for c in reversed(first14):
        addend = factor * _GST_CHARS.index(c)
        factor = 1 if factor == 2 else 2
        total += addend // 36 + addend % 36
    return _GST_CHARS[(36 - total % 36) % 36]


def gstin_problem(g: str | None) -> str | None:
    """None if valid, else a plain-language problem."""
    if not g:
        return None
    if not GSTIN_RE.match(g):
        return "GSTIN format is not valid (should be 15 characters like 27ABCDE1234F1Z5)"
    if g[:2] not in STATE_CODES:
        return f"GSTIN state code {g[:2]} does not exist"
    if gstin_check_char(g[:14]) != g[14]:
        return "GSTIN check digit does not match: a character was probably misread"
    return None


def state_code_for(text: str | None) -> str | None:
    """Accepts '27', '27-Maharashtra', 'Maharashtra (27)', 'MAHARASHTRA'."""
    if not text:
        return None
    t = str(text).strip()
    m = re.search(r"\b(\d{2})\b", t)
    if m and m.group(1) in STATE_CODES:
        return m.group(1)
    low = re.sub(r"[^a-z& ]", "", t.lower()).strip()
    if low in _STATE_ALIASES:
        return _STATE_ALIASES[low]
    for code, name in STATE_CODES.items():
        if name.lower() == low or (len(low) > 4 and low in name.lower()):
            return code
    return None


# --- Amount in words (Indian English) ------------------------------------------------------------

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000,
           "lacs": 100_000, "crore": 10_000_000, "crores": 10_000_000, "million": 1_000_000,
           "billion": 1_000_000_000}


def _words_to_int(words: list[str]) -> int | None:
    total, current, seen = 0, 0, False
    for w in words:
        if w in _UNITS:
            current += _UNITS[w]
            seen = True
        elif w in _TENS:
            current += _TENS[w]
            seen = True
        elif w == "hundred":
            current = max(current, 1) * 100
        elif w in _SCALES:
            total += max(current, 1) * _SCALES[w]
            current = 0
            seen = True
        elif w in ("and", "only", "rupees", "rupee", "rs", "inr", "indian", "the", "sum", "of"):
            continue
        else:
            return None
    return total + current if seen else None


def words_to_amount(text: str | None) -> Decimal | None:
    if not text:
        return None
    t = re.sub(r"[^a-z ]", " ", str(text).lower().replace("-", " "))
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    rupee_part, paise_part = t, ""
    if "paise" in t or "paisa" in t:
        m = re.split(r"\band\b(?=[^a-z]*[a-z ]*pais[ea])", t, maxsplit=1)
        if len(m) == 2:
            rupee_part, paise_part = m
        else:
            rupee_part, paise_part = "", t
        paise_part = paise_part.replace("paise", "").replace("paisa", "")
    rupees = _words_to_int(rupee_part.split()) if rupee_part.strip() else 0
    if rupees is None:
        return None
    paise = _words_to_int(paise_part.split()) if paise_part.strip() else 0
    if paise is None:
        return None
    return Decimal(rupees) + Decimal(paise) / 100
