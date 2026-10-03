"""The second, independent reading: zoomed crops of exactly the fields nothing else could prove.

After the page reading, deterministic evidence (evidence.py) proves most values for free. The
rest - high-stakes values without proof, anything not clearly legible, and on hard documents every
present value - are re-read in ONE request: each crop is cut around the location the first
reading reported, from an ink-enhanced variant of the original pixels, and read by a different
model that is never shown the first reading. Two readings that agree are strong evidence; two that
differ go to a person with both values on screen (no third model call). A crop that no longer
contains a value also catches a value the first reading invented.

Basis: zooming into small regions lifts multimodal models' reading of fine print (Zhang et al.,
ICLR 2025); two structurally different readings separate right from wrong extractions where
logprobs, verbalised confidence and repeated sampling do not (ExtractConf, 2026).
"""
from __future__ import annotations

import cv2
import numpy as np

from sereno.extraction.llm import LLMClient, LLMResult, image_block, text_block
from sereno.extraction.normalize import normalise, values_agree
from sereno.extraction.result import ExtractedDoc, FieldValue
from sereno.ingest.loader import encode_jpeg
from sereno.ingest.preprocess import fit_long_edge

CROP_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["reads"],
    "properties": {"reads": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["crop", "value", "raw_text", "legibility"],
        "properties": {"crop": {"type": "integer"}, "value": {"type": "string"}, "raw_text": {"type": "string"},
                       "legibility": {"enum": ["clear", "partly_legible", "illegible", "not_present"]}}}}},
}

CROP_SYSTEM = """\
You read values from zoomed images cut from Indian business documents (GST invoices, lorry
receipts, purchase orders, goods receipt notes). Each numbered item names one field or one column
of a line-item row and the image to read it from. Read ONLY that item, directly from the pixels,
character by character. Nobody else's reading is shown to you: read it fresh.
- value: normalised. Numbers: digits with "." as decimal separator, no commas or currency
  (Indian grouping 1,23,456.00 is 123456.00). Dates: YYYY-MM-DD; Indian dates are day-first.
  Codes (GSTIN, PAN, IFSC, HSN, vehicle, e-way bill): uppercase, no spaces. Indic numerals -> 0-9.
- raw_text: exactly as printed or written, including Devanagari.
- legibility: "partly_legible" if any character is uncertain; "illegible" if present but
  unreadable; "not_present" if the item is not in the image. Never guess a value.
For a line-item row image, read only the named column of that row.
Look-alikes need care: 0/O/D, 1/I/7, 5/S, 8/B, 2/Z in codes; 3/8, 1/7, 4/9 in handwriting.
"""


def ink_variant(img: np.ndarray) -> np.ndarray:
    """Grey, locally contrast-equalised, slightly sharpened: differs from the colour-enhanced page
    the first reading saw, so the two readings don't share preprocessing artefacts."""
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
    blur = cv2.GaussianBlur(gray, (0, 0), 1.0)
    gray = cv2.addWeighted(gray, 1.5, blur, -0.5, 0)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)


def _crop(img: np.ndarray, bbox: dict, row: bool) -> np.ndarray | None:
    h, w = img.shape[:2]
    bw, bh = bbox["x1"] - bbox["x0"], bbox["y1"] - bbox["y0"]
    padx = 0.01 if row else max(0.05, bw * 0.5)
    pady = max(0.012, bh * (0.35 if row else 1.2))
    x0, x1 = int(max(0, bbox["x0"] - padx) * w), int(min(1, bbox["x1"] + padx) * w)
    y0, y1 = int(max(0, bbox["y0"] - pady) * h), int(min(1, bbox["y1"] + pady) * h)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = img[y0:y1, x0:x1]
    target = 1400 if row else 1000
    s = min(4.0, target / max(crop.shape[1], 1))
    if s > 1.05:
        crop = cv2.resize(crop, (int(crop.shape[1] * s), int(crop.shape[0] * s)), interpolation=cv2.INTER_CUBIC)
    return crop


def needs_reread(fv: FieldValue, hard: bool) -> bool:
    if fv.verified_by or fv.code_filled:
        return False
    if fv.value is None:
        return fv.legibility in ("illegible", "partly_legible")  # present but not read: try zoomed
    return (fv.spec.high_stakes or hard or fv.legibility != "clear" or fv.raw_consistent is False
            or fv.unparseable)


def select_targets(doc: ExtractedDoc, hard: bool, limit: int) -> list[FieldValue]:
    pool = [f for f in doc.all_fields() if needs_reread(f, hard)]
    pool.sort(key=lambda f: (not f.spec.high_stakes, f.legibility == "clear", f.line_index is not None,
                             f.line_index or 0))
    return pool[:limit]


def run_crop_reads(llm: LLMClient, fields: list[FieldValue], pages, model: str,
                   effort: str | None = None) -> tuple[LLMResult | None, dict]:
    """One request for all targets. Each distinct region is sent once (a line-item row serves all
    its columns); a value with no location is read from the full page. Returns
    (result, {field key: (value, raw_text, legibility)})."""
    by_page = {p.page_no: p for p in pages}
    variants: dict[int, np.ndarray] = {}
    content: list[dict] = []
    regions: dict[tuple, int] = {}
    index: dict[int, FieldValue] = {}
    for fv in fields:
        page_no = fv.page if fv.page in by_page else (min(by_page) if len(by_page) == 1 else None)
        if page_no is None:
            continue
        if page_no not in variants:
            variants[page_no] = ink_variant(by_page[page_no].original)
        region = (page_no, tuple(round(v, 4) for v in fv.bbox.values()) if fv.bbox else None)
        if region not in regions:
            img = (_crop(variants[page_no], fv.bbox, row=fv.line_index is not None) if fv.bbox
                   else fit_long_edge(variants[page_no], 2000))
            if img is None:
                continue
            regions[region] = len(regions) + 1
            what = "a zoomed region" if fv.bbox else f"the full page {page_no}"
            content.append(text_block(f"Image {regions[region]} ({what}):"))
            content.append(image_block(encode_jpeg(img, 92), "image/jpeg"))
        k = len(index) + 1
        what = (f"line {fv.line_index + 1}, column '{fv.spec.label}'" if fv.line_index is not None
                else f"the field '{fv.spec.label}'")
        hint = f" ({fv.spec.hint})" if fv.spec.hint else ""
        content.append(text_block(f'Crop {k} [key "{fv.key}"]: from image {regions[region]}, read {what} '
                                  f'[{fv.spec.type}]{hint}.'))
        index[k] = fv
    if not index:
        return None, {}
    content.append(text_block("Return one read per numbered item, using the item numbers."))
    res = llm.structured(pass_name="crop", model=model, system=CROP_SYSTEM, content=content, schema=CROP_SCHEMA,
                         max_tokens=16000, effort=effort)
    out = {}
    for r in res.data.get("reads") or []:
        fv = index.get(r.get("crop"))
        if fv is None:
            continue
        leg = r.get("legibility", "not_present")
        val = None if leg in ("not_present", "illegible") else normalise(fv.spec.type, r.get("value"))
        out[fv.key] = (val, r.get("raw_text") or "", leg)
    return res, out


def apply_reads(doc: ExtractedDoc, reads: dict) -> list[FieldValue]:
    """Compare each zoomed reading with the page reading. Returns the fields that differ."""
    by_key = doc.by_key()
    differ = []
    for key, (z_val, z_raw, z_leg) in reads.items():
        fv = by_key.get(key)
        if fv is None:
            continue
        fv.double_read = True
        if z_leg == "illegible":
            fv.zoom_illegible = True
            fv.legibility = "partly_legible" if fv.legibility == "clear" else fv.legibility
            fv.agreement = fv.value is None
            continue
        if fv.value is None and z_val is not None:
            # the page reading missed it; the zoomed reading found it: a person confirms the find
            fv.value, fv.raw_text, fv.agreement = z_val, z_raw, False
            fv.alt_value = None
            fv.evidence.append("found on zoom")
            differ.append(fv)
            continue
        if values_agree(fv.spec.type, fv.value, z_val):
            fv.agreement = True
            fv.evidence.append("confirmed on zoom")
        else:
            fv.agreement, fv.alt_value = False, z_val
            fv.evidence.append("zoomed reading differs" if z_val is not None else "not found on zoom")
            differ.append(fv)
    return differ
