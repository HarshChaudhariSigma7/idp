"""Third, blind reading of chosen fields from zoomed crops, plus majority voting.

Pass A and pass B see whole pages. When they disagree, or when the document is hard (handwriting,
Indic script, poor scan), each field is read a third time from a tight, zoomed crop of an
ink-enhanced variant of the page. The crop reader is never shown the earlier readings, so it
cannot be anchored by them. Two of three agreeing readings pick the value; the field still goes
to a person whenever any reading dissented.
"""
from __future__ import annotations

import cv2
import numpy as np

from sereno.extraction.llm import LLMClient, LLMResult, image_block, text_block
from sereno.extraction.normalize import normalise, values_agree
from sereno.extraction.result import ExtractedDoc, FieldValue
from sereno.ingest.loader import encode_jpeg

MAX_CROPS = 24

CROP_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["reads"],
    "properties": {"reads": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["crop", "value", "raw_text", "legibility"],
        "properties": {"crop": {"type": "integer"}, "value": {"type": "string"}, "raw_text": {"type": "string"},
                       "legibility": {"enum": ["clear", "partly_legible", "illegible", "not_present"]}}}}},
}

CROP_SYSTEM = """\
You read small zoomed crops cut from Indian business documents (invoices, lorry receipts, GRNs).
For each numbered crop you are told which field or column to read. Read ONLY that item, directly
from the pixels, character by character. You are not told what anyone else read, so read it fresh.
- value: normalised (numbers: digits with "." decimal, no commas or currency; dates YYYY-MM-DD,
  Indian dates are day-first; codes uppercase without spaces; Indic numerals converted to 0-9).
- raw_text: exactly as printed or written, including Devanagari.
- legibility: be honest; "partly_legible" if any character is uncertain; "illegible" if you cannot
  read it; "not_present" if the item is not in the crop. Never guess a value.
For a line-item crop, the whole row is shown: read only the named column of that row.
"""


def ink_variant(img: np.ndarray) -> np.ndarray:
    """Grey, locally contrast-equalised, slightly sharpened: a third view that differs from both the
    colour-enhanced page (pass A) and the untouched original (pass B)."""
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


def run_crop_reads(llm: LLMClient, fields: list[FieldValue], pages, model: str) -> tuple[LLMResult | None, dict]:
    """Returns (llm result, {field key: (value, raw_text, legibility)})."""
    by_page = {p.page_no: p for p in pages}
    variants: dict[int, np.ndarray] = {}
    content: list[dict] = []
    index: dict[int, FieldValue] = {}
    for fv in fields:
        if len(index) >= MAX_CROPS or not fv.bbox or fv.page not in by_page:
            continue
        if fv.page not in variants:
            variants[fv.page] = ink_variant(by_page[fv.page].original)
        crop = _crop(variants[fv.page], fv.bbox, row=fv.line_index is not None)
        if crop is None:
            continue
        k = len(index) + 1
        what = (f"line {fv.line_index + 1}, column '{fv.spec.label}'" if fv.line_index is not None
                else f"the field '{fv.spec.label}'")
        hint = f" ({fv.spec.hint})" if fv.spec.hint else ""
        content.append(text_block(f'Crop {k} [key "{fv.key}"]: read {what} [{fv.spec.type}]{hint}.'))
        content.append(image_block(encode_jpeg(crop, 92), "image/jpeg"))
        index[k] = fv
    if not index:
        return None, {}
    content.append(text_block("Return one read per crop, using the crop numbers."))
    res = llm.structured(pass_name="crop", model=model, system=CROP_SYSTEM, content=content, schema=CROP_SCHEMA,
                         max_tokens=16000)
    out = {}
    for r in res.data.get("reads") or []:
        fv = index.get(r.get("crop"))
        if fv is None:
            continue
        leg = r.get("legibility", "not_present")
        val = None if leg in ("not_present", "illegible") else normalise(fv.spec.type, r.get("value"))
        out[fv.key] = (val, r.get("raw_text") or "", leg)
    return res, out


def apply_votes(doc: ExtractedDoc, reads: dict) -> list[FieldValue]:
    """Majority vote over A (pass A), B (pass B) and C (crop). Returns fields still unresolved."""
    by_key = doc.by_key()
    unresolved = []
    for key, (c_val, c_raw, c_leg) in reads.items():
        fv = by_key.get(key)
        if fv is None:
            continue
        fv.third_value = c_val
        t = fv.spec.type
        a = fv.value
        b = fv.alt_value if fv.double_read else a
        if c_leg == "illegible":
            fv.votes = "illegible on zoom"
            fv.legibility = "partly_legible" if fv.legibility == "clear" else fv.legibility
            if fv.agreement is False:
                unresolved.append(fv)
            continue
        ab, ac, bc = values_agree(t, a, b), values_agree(t, a, c_val), values_agree(t, b, c_val)
        if ab and ac:
            fv.votes = "3/3"
            fv.evidence.append("3 of 3 readings agree")
        elif ac or bc:
            winner = a if ac else b
            fv.votes = "2/3"
            fv.evidence.append("2 of 3 readings agree")
            if not values_agree(t, fv.value, winner):
                fv.alt_value, fv.value = fv.value, winner
                fv.raw_text = c_raw or fv.raw_text
            fv.agreement = False  # a reading dissented: a person confirms
        elif ab:
            fv.votes = "2/3"
            fv.agreement = False
            fv.alt_value = c_val
            fv.evidence.append("zoomed re-read differs")
        else:
            fv.votes = "1/1/1"
            fv.agreement = False
            unresolved.append(fv)
    return unresolved
