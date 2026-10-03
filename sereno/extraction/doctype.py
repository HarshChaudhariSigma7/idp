"""Free signals for what a document is, so most uploads need no separate classification call.

Order of trust: the type the user chose, a signed e-invoice QR, the PDF text layer, and finally
the company's most common type as a guess that the page reading confirms (it reports the type it
actually sees; a wrong guess costs one repeated reading, a separate triage call would cost one on
every document).
"""
from __future__ import annotations

import re

_KEYWORDS: dict[str, dict[str, int]] = {
    "invoice": {"tax invoice": 5, "bill of supply": 5, "invoice no": 3, "invoice number": 3, "inv no": 2,
                "invoice date": 2, "e-invoice": 2, "irn": 1, "कर बीजक": 5, "बीजक": 3},
    "lr": {"lorry receipt": 6, "consignment note": 6, "bilty": 5, "builty": 5, "l.r. no": 5, "lr no": 4,
           "g.r. no": 4, "goods consignment": 4, "consignor": 3, "truck no": 2, "freight": 2, "बिल्टी": 5},
    "po": {"purchase order": 6, "p.o. no": 3, "po number": 3, "po no": 2, "delivery schedule": 2},
    "grn": {"goods receipt note": 6, "goods received note": 6, "grn no": 5, "material inward": 5, "inward note": 4,
            "accepted qty": 3, "rejected qty": 3, "mrn": 2},
    "contract": {"this agreement": 4, "hereinafter": 4, "witnesseth": 4, "rate contract": 5, "whereas": 3,
                 "agreement": 2, "effective date": 2},
}

_NUMBER = re.compile(r"(?:invoice|inv|bill|l\.?r|g\.?r|p\.?o|grn)\.?\s*(?:no|number|#)\.?\s*[:\-]?\s*"
                     r"([A-Z0-9][A-Z0-9/\-]{2,})", re.I)
_GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")


def from_text(text: str) -> str | None:
    """Keyword vote over the text layer; the title area counts double. None when unsure."""
    t = re.sub(r"\s+", " ", (text or "").lower())
    if len(t) < 40:
        return None
    head = t[:400]
    scores = {k: sum(w * (t.count(kw) > 0) + w * (kw in head) for kw, w in kws.items()) for k, kws in _KEYWORDS.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (top, s1), (_, s2) = ranked[0], ranked[1]
    return top if s1 >= 5 and s1 >= 1.6 * max(s2, 1) else None


def local_groups(page_texts: list[str]) -> list[list[int]] | None:
    """Split a digital multi-page file by document number (a page with a new number starts a new
    document; pages without one continue the current). None when the text says nothing."""
    numbers = []
    for t in page_texts:
        m = _NUMBER.search(t or "")
        numbers.append(m.group(1).upper() if m else None)
    if not any(numbers):
        return None
    groups: list[list[int]] = []
    current = None
    for i, n in enumerate(numbers, 1):
        if not groups or (n is not None and current is not None and n != current):
            groups.append([i])
        else:
            groups[-1].append(i)
        current = n or current
    return groups


def issuer_gstin(text: str, own: set[str]) -> str | None:
    """First GSTIN in the text that isn't the company's own: the issuer, for vendor templates."""
    for g in _GSTIN.findall((text or "").upper()):
        if g not in own:
            return g
    return None
