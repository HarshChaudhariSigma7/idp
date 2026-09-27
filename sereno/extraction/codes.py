"""Machine-readable codes printed on Indian business documents: exact data, no model error.

* GST e-invoice QR (mandatory for suppliers above the e-invoicing threshold): a JWT signed by the
  NIC Invoice Registration Portal carrying seller/buyer GSTIN, document number, date, total value,
  item count, main HSN and the IRN. The signature makes it tamper-evident: if the printed page
  disagrees with a correctly-read QR, the printed page was edited or misread.
* UPI payment QR: payee name and often the amount (and sometimes the invoice number as `tr`).
* 1-D barcodes (e-invoice acknowledgement no., e-way bill no., LR no.): matched against fields.

Found on real public samples: a sample e-invoice whose printed GSTINs and IRN had been edited
while its signed QR still carried the originals. Decoding is local, fast and free.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

from sereno.extraction.normalize import clean_code, normalise, values_agree
from sereno.extraction.result import ExtractedDoc

log = logging.getLogger("sereno.codes")

try:  # zxing-cpp decodes dense e-invoice QRs that OpenCV misses (verified on real samples)
    import zxingcpp
except ImportError:  # pragma: no cover
    zxingcpp = None


@dataclass
class CodeReading:
    source: str                      # einvoice_qr | upi_qr | barcode
    page: int
    fields: dict = field(default_factory=dict)   # spec field name -> normalised value
    extra: dict = field(default_factory=dict)    # item_count, doc_type, main_hsn, signature status
    raw_text: str = ""


def decode_image(img: np.ndarray) -> list[tuple[str, str]]:
    """[(format, text)] for every QR/barcode found. Keeps trying larger scales until a dense
    e-invoice QR is found: on degraded scans it often decodes only at 2-3x (verified), while a
    sparse UPI QR on the same page decodes at 1x."""
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    found: dict[str, str] = {}
    for scale in (1.0, 2.0, 3.0):
        if scale * max(gray.shape) > 9000:
            break
        im = gray if scale == 1.0 else cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        texts: list[tuple[str, str]] = []
        if zxingcpp is not None:
            try:
                texts = [(r.format.name, r.text) for r in zxingcpp.read_barcodes(im) if r.text]
            except Exception:  # noqa: BLE001  never let a decoder crash extraction
                texts = []
        if not texts:
            try:
                ok, qrs, _, _ = cv2.QRCodeDetectorAruco().detectAndDecodeMulti(im)
                texts = [("QRCode", t) for t in (qrs or []) if t]
            except Exception:  # noqa: BLE001
                texts = []
        for fmt, t in texts:
            found.setdefault(t, fmt)
        if any(t.startswith("eyJ") for t in found):
            break
    return [(fmt, t) for t, fmt in found.items()]


def _b64json(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def verify_jwt(token: str, public_keys_pem: list[str]) -> bool | None:
    """True/False when NIC public keys are configured, None when verification is not possible."""
    if not public_keys_pem:
        return None
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    head, body, sig = token.split(".")
    signature = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    for pem in public_keys_pem:
        try:
            key = serialization.load_pem_public_key(pem.encode())
            key.verify(signature, f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def parse_einvoice(text: str, page: int, public_keys_pem: list[str] | None = None) -> CodeReading | None:
    if not text.startswith("eyJ") or text.count(".") != 2:
        return None
    try:
        payload = _b64json(text.split(".")[1])
        data = payload.get("data")
        data = json.loads(data) if isinstance(data, str) else data
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or "SellerGstin" not in data:
        return None
    f = {
        "supplier_gstin": normalise("gstin", data.get("SellerGstin")),
        "buyer_gstin": normalise("gstin", data.get("BuyerGstin")),
        "invoice_number": normalise("string", data.get("DocNo")),
        "invoice_date": normalise("date", data.get("DocDt")),
        "grand_total": normalise("number", str(data.get("TotInvVal")) if data.get("TotInvVal") is not None else None),
        "irn": normalise("string", data.get("Irn")),
    }
    return CodeReading("einvoice_qr", page, {k: v for k, v in f.items() if v is not None},
                       {"item_count": data.get("ItemCnt"), "doc_type": data.get("DocTyp"),
                        "main_hsn": clean_code(data.get("MainHsnCode")), "issuer": payload.get("iss"),
                        "signature_verified": verify_jwt(text, public_keys_pem or [])}, text[:40])


def parse_upi(text: str, page: int) -> CodeReading | None:
    if not text.lower().startswith("upi://pay"):
        return None
    q = {k: v[0] for k, v in parse_qs(urlparse(text).query).items()}
    f = {}
    am = normalise("number", q.get("am"))
    if am and am > 0:
        f["grand_total"] = am
    name = unquote(q.get("pn", "")).strip()
    return CodeReading("upi_qr", page, f, {"payee_name": name or None, "reference": q.get("tr") or q.get("tn"),
                                           "vpa": q.get("pa")}, text[:40])


def read_codes(images: list[tuple[int, np.ndarray]], public_keys_pem: list[str] | None = None) -> list[CodeReading]:
    out: list[CodeReading] = []
    for page, img in images:
        for fmt, text in decode_image(img):
            r = parse_einvoice(text, page, public_keys_pem) or parse_upi(text, page)
            if r is None and fmt != "QRCode" and re.fullmatch(r"[A-Za-z0-9/\-]{5,40}", text or ""):
                r = CodeReading("barcode", page, {}, {"value": clean_code(text)}, text)
            if r is not None:
                out.append(r)
    return out


def _names_match(a: str | None, b: str | None) -> bool | None:
    if not a or not b:
        return None
    from rapidfuzz import fuzz
    squash = lambda t: re.sub(r"[^a-z0-9]", "", t.lower().replace("pvt", "private").replace("ltd", "limited"))  # noqa: E731
    return fuzz.ratio(squash(a), squash(b)) >= 85


def apply_codes(doc: ExtractedDoc, readings: list[CodeReading]) -> list[str]:
    """Attach code values to fields. Returns anomaly notes. Never silently overwrites a legible
    printed value: disagreements are kept as the alternative and routed to a person; only a field
    that was absent or illegible on paper is filled from the code (and tagged as such)."""
    doc.codes = readings
    notes: list[str] = []
    spec_fields = {f.name for f in doc.spec.fields}
    for r in readings:
        weight_source = r.source
        for name, code_val in r.fields.items():
            if name not in spec_fields or code_val is None:
                continue
            fv = doc.header[name]
            if r.source == "upi_qr" and fv.code_source == "einvoice_qr":
                continue  # the signed e-invoice QR outranks a payment QR
            fv.code_value, fv.code_source = code_val, weight_source
            if fv.value is None or fv.legibility == "illegible":
                fv.value, fv.code_filled, fv.code_agrees = code_val, True, True
                fv.evidence.append("read from QR" if r.source != "barcode" else "read from barcode")
            else:
                fv.code_agrees = values_agree(fv.spec.type, fv.value, code_val)
                if fv.code_agrees:
                    fv.evidence.append("matches e-invoice QR" if r.source == "einvoice_qr" else "matches UPI QR")
                elif fv.alt_value is None or not values_agree(fv.spec.type, fv.alt_value, code_val):
                    fv.alt_value = code_val if fv.alt_value is None else fv.alt_value
        if r.source == "einvoice_qr":
            if r.extra.get("signature_verified") is False:
                notes.append("E-invoice QR signature did not verify against the NIC key: treat the document as suspect")
            if r.extra.get("doc_type") in ("CRN", "DBN"):
                notes.append(f"E-invoice QR marks this as a {'credit' if r.extra['doc_type'] == 'CRN' else 'debit'} note")
            if r.extra.get("item_count"):
                doc.row_counts["qr"] = int(r.extra["item_count"])
        if r.source == "upi_qr" and "supplier_name" in spec_fields:
            match = _names_match(doc.v("supplier_name"), r.extra.get("payee_name"))
            if match:
                doc.header["supplier_name"].evidence.append("matches UPI payee name")
            elif match is False:
                notes.append(f"UPI payee name '{r.extra.get('payee_name')}' differs from the vendor name: check before paying")
        if r.source == "barcode":
            val = r.extra.get("value")
            for fv in doc.header.values():
                if fv.value is not None and fv.spec.type in ("string", "gstin", "vehicle") and clean_code(fv.value) == val:
                    fv.code_value, fv.code_source, fv.code_agrees = fv.value, "barcode", True
                    fv.evidence.append("matches barcode")
    return notes
