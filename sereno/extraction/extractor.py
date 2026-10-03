"""The page reading: one structured request per document.

The full schema is filled from the (enhanced, margin-trimmed) page images, plus the native text
layer for digital PDFs, with a location for every value. What it says is then checked
deterministically (evidence.py) and only the unproven values are re-read (crossread.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from sereno.extraction import prompts
from sereno.extraction.doc_specs import DocSpec, primary_schema
from sereno.extraction.llm import LLMClient, LLMResult, image_block, text_block
from sereno.extraction.normalize import clean_code, fold_digits, normalise, parse_amount, raw_consistent
from sereno.extraction.result import ExtractedDoc, FieldValue, line_key
from sereno.ingest.loader import Word, encode_jpeg
from sereno.ingest.preprocess import content_frame, cut, fit_long_edge


@dataclass
class PreparedPage:
    page_no: int
    enhanced: np.ndarray
    original: np.ndarray
    native_text: bool = False
    text: str = ""
    words: list[Word] = field(default_factory=list)


def _budget_edge(long_edge: int, n_pages: int) -> int:
    """API limits: 100 images and 32 MB per request; long documents get smaller pages."""
    return long_edge if n_pages <= 6 else min(long_edge, 1800 if n_pages <= 15 else 1400)


def run_primary(llm: LLMClient, spec: DocSpec, pages: list[PreparedPage], model: str, long_edge: int,
                template_hints: str | None = None, effort: str | None = None) -> tuple[LLMResult, dict]:
    """Returns (result, frames): frames maps page -> the trimmed area sent, so locations the model
    reports can be mapped back onto the full page."""
    content: list[dict] = []
    frames: dict[int, tuple] = {}
    long_edge = _budget_edge(long_edge, len(pages))
    for p in pages:
        frames[p.page_no] = (0.0, 0.0, 1.0, 1.0) if p.native_text else content_frame(p.enhanced)
        content.append(text_block(f"Page {p.page_no} of {len(pages)}:"))
        content.append(image_block(encode_jpeg(fit_long_edge(cut(p.enhanced, frames[p.page_no]), long_edge), 90),
                                   "image/jpeg"))
    if any(p.native_text for p in pages):
        content.append(text_block(prompts.text_layer_block([p.text for p in pages])))
    if template_hints:
        content.append(text_block(prompts.template_block(template_hints)))
    content.append(text_block("Extract every field and line item into the required JSON structure."))
    res = llm.structured(pass_name="primary", model=model, system=prompts.primary_system(spec), content=content,
                         schema=primary_schema(spec), effort=effort)
    return res, frames


def remap_locations(doc: ExtractedDoc, frames: dict) -> None:
    """Locations are relative to the trimmed image the model saw; convert to full-page fractions."""
    seen: set[int] = set()
    for fv in doc.all_fields():
        b, fr = fv.bbox, frames.get(fv.page)
        if not b or not fr or fr == (0.0, 0.0, 1.0, 1.0) or id(b) in seen:
            continue
        seen.add(id(b))
        x0, y0, x1, y1 = fr
        b["x0"], b["x1"] = x0 + b["x0"] * (x1 - x0), x0 + b["x1"] * (x1 - x0)
        b["y0"], b["y1"] = y0 + b["y0"] * (y1 - y0), y0 + b["y1"] * (y1 - y0)


def _bbox(b: dict | None) -> dict | None:
    if not b:
        return None
    try:
        x0, y0, x1, y1 = (max(0.0, min(1.0, float(b[k]))) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    if x1 - x0 <= 0.001 or y1 - y0 <= 0.001:
        return None
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _mk_field(key: str, spec_f, line_index, cell: dict, page: int | None, bbox: dict | None) -> FieldValue:
    raw_value = cell.get("value", "")
    legibility = cell.get("legibility", "not_present")
    value = None if legibility == "not_present" else normalise(spec_f.type, raw_value)
    fv = FieldValue(key=key, spec=spec_f, line_index=line_index, value=value,
                    raw_text=cell.get("raw_text", "") or "", legibility=legibility,
                    page=page if page else None, bbox=bbox)
    fv.unparseable = bool(str(raw_value).strip()) and value is None and legibility != "not_present"
    fv.raw_consistent = raw_consistent(spec_f.type, value, fv.raw_text)
    return fv


def parse_primary(spec: DocSpec, data: dict) -> ExtractedDoc:
    fields = data.get("fields", {})
    header = {}
    for f in spec.fields:
        cell = fields.get(f.name) or {}
        header[f.name] = _mk_field(f.name, f, None, cell, cell.get("page") or None, _bbox(cell.get("bbox")))
    lines = []
    for i, row in enumerate(data.get("line_items") or []):
        cells = row.get("cells", {})
        page, rb = row.get("page") or None, _bbox(row.get("row_bbox"))
        lines.append({f.name: _mk_field(line_key(i, f.name), f, i, cells.get(f.name) or {}, page, rb)
                      for f in spec.line_fields})
    return ExtractedDoc(spec=spec, header=header, lines=lines, languages=list(data.get("languages") or []),
                        handwriting=bool(data.get("handwriting_present")),
                        legibility=data.get("overall_legibility", "clear"),
                        anomalies=[a for a in (data.get("anomalies") or []) if a][:20],
                        observed_type=data.get("document_type_observed"))


# --- text layer grounding & location refinement ----------------------------------------------------

_NUM_TOKEN = re.compile(r"[\d०-९][\d०-९,]*(?:\.[\d०-९]+)?")
_DATE_TOKEN = re.compile(r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}|\d{1,2}[\s-]*[A-Za-z]{3,9}[\s,-]*\d{2,4}")


def ground_in_text_layer(doc: ExtractedDoc, pages: list[PreparedPage]) -> None:
    """For digital PDFs: is the extracted value literally present in the text layer?"""
    if not pages or not all(p.native_text for p in pages):
        return
    full = "\n".join(p.text for p in pages)
    numbers = {round(float(v), 2) for v in (parse_amount(t) for t in _NUM_TOKEN.findall(fold_digits(full))) if v is not None}
    codes = clean_code(full)
    squashed = re.sub(r"\s+", " ", full).lower()
    dates = {normalise("date", t) for t in _DATE_TOKEN.findall(fold_digits(full))} - {None}
    for fv in doc.all_fields():
        if fv.value is None:
            continue
        t = fv.spec.type
        if t in ("number", "percent", "integer"):
            fv.in_text_layer = round(abs(float(fv.value)), 2) in numbers or round(float(fv.value), 2) in numbers
        elif t in ("gstin", "pan", "ifsc", "vehicle", "hsn"):
            fv.in_text_layer = str(fv.value) in codes
        elif t == "string" and len(str(fv.value)) >= 3:
            fv.in_text_layer = str(fv.value).lower() in squashed
        elif t == "date":
            fv.in_text_layer = fv.value in dates
    refine_locations(doc, pages)


def refine_locations(doc: ExtractedDoc, pages: list[PreparedPage]) -> None:
    """Snap the model's approximate box to the exact text-layer word box when one matches."""
    by_page = {p.page_no: p for p in pages}
    for fv in doc.header.values():
        if fv.value is None or not fv.raw_text or fv.page not in by_page:
            continue
        words = by_page[fv.page].words
        target = re.sub(r"\s+", "", fv.raw_text)
        cands = [w for w in words if w.text and (w.text == target or (len(target) >= 4 and target in w.text))]
        if not cands:
            continue
        if fv.bbox:
            cx, cy = (fv.bbox["x0"] + fv.bbox["x1"]) / 2, (fv.bbox["y0"] + fv.bbox["y1"]) / 2
            w = min(cands, key=lambda w: ((w.x0 + w.x1) / 2 - cx) ** 2 + ((w.y0 + w.y1) / 2 - cy) ** 2)
        else:
            w = cands[0]
        fv.bbox = {"x0": w.x0, "y0": w.y0, "x1": w.x1, "y1": w.y1}
