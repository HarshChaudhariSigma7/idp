"""Multi-pass extraction.

Pass A (primary): full schema, enhanced page images (+ native text layer for digital PDFs),
                  with locations for the review screen.
Pass B (secondary): every field again, different framing, ORIGINAL (un-enhanced) pixels as
                  zoomed overlapping strips. Independent errors => disagreement is informative.
Verifier:        runs only on disagreements, sees zoomed crops, picks A / B / neither.
Disagreement always lowers confidence, even after the verifier picks a side.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from sereno.extraction import prompts
from sereno.extraction.doc_specs import DocSpec, VERIFY_SCHEMA, primary_schema, secondary_schema
from sereno.extraction.llm import LLMClient, LLMResult, image_block, text_block
from sereno.extraction.normalize import (clean_code, fold_digits, normalise, parse_amount, raw_consistent,
                                         values_agree)
from sereno.extraction.result import ExtractedDoc, FieldValue, line_key
from sereno.ingest.loader import Word, encode_jpeg
from sereno.ingest.preprocess import fit_long_edge, tiles


@dataclass
class PreparedPage:
    page_no: int
    enhanced: np.ndarray
    original: np.ndarray
    native_text: bool = False
    text: str = ""
    words: list[Word] = field(default_factory=list)


# API limits: at most 100 images and 32 MB per request. Long documents (contracts) get fewer,
# smaller images; short commercial documents get zoomed strips for the independent re-read.
MAX_PAGES_WITH_STRIPS = 10


def _budget_edge(long_edge: int, n_pages: int) -> int:
    return long_edge if n_pages <= 6 else (1800 if n_pages <= 15 else 1400)


def _img(img: np.ndarray, long_edge: int) -> dict:
    return image_block(encode_jpeg(fit_long_edge(img, long_edge), 90), "image/jpeg")


# --- pass A --------------------------------------------------------------------------------------

def run_primary(llm: LLMClient, spec: DocSpec, pages: list[PreparedPage], model: str,
                long_edge: int, template_hints: str | None = None, effort: str | None = None) -> LLMResult:
    content: list[dict] = []
    long_edge = _budget_edge(long_edge, len(pages))
    for p in pages:
        content.append(text_block(f"Page {p.page_no} of {len(pages)}:"))
        content.append(_img(p.enhanced, long_edge))
    if any(p.native_text for p in pages):
        content.append(text_block(prompts.text_layer_block([p.text for p in pages])))
    content.append(text_block(prompts.field_guide(spec, template_hints) +
                              "\n\nExtract every field and line item into the required JSON structure."))
    return llm.structured(pass_name="primary", model=model, system=prompts.PRIMARY_SYSTEM, content=content,
                          schema=primary_schema(spec), effort=effort)


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


# --- pass B --------------------------------------------------------------------------------------

def run_secondary(llm: LLMClient, spec: DocSpec, pages: list[PreparedPage], model: str, long_edge: int) -> LLMResult:
    content: list[dict] = []
    long_edge = _budget_edge(long_edge, len(pages))
    use_strips = len(pages) <= MAX_PAGES_WITH_STRIPS
    for p in pages:
        if use_strips:
            strips = tiles(p.original, n=2 if p.original.shape[0] < 2600 else 3)
            for j, strip in enumerate(strips, 1):
                content.append(text_block(f"Page {p.page_no}, strip {j} of {len(strips)} (top to bottom):"))
                content.append(_img(strip, long_edge))
        content.append(text_block(f"Page {p.page_no}, full page{' for context' if use_strips else ''}:"))
        content.append(_img(p.original, 1400 if use_strips else long_edge))
    guide = ["Re-key these fields:"] + [f"- {f.name}: {f.label} [{f.type}]" + (f" ({f.hint})" if f.hint else "") for f in spec.fields]
    if spec.line_fields:
        guide += ["", "And for every line item row:"] + [f"- {f.name}: {f.label} [{f.type}]" for f in spec.line_fields]
    content.append(text_block("\n".join(guide)))
    return llm.structured(pass_name="secondary", model=model, system=prompts.SECONDARY_SYSTEM, content=content,
                          schema=secondary_schema(spec))


def _align_lines(a_lines: list[dict], b_lines: list[dict], spec: DocSpec) -> list[int | None]:
    """Map each A line to a B line. Same count => positional. Otherwise best match on amounts AND
    description, so a row missed by one reading shifts nothing (every later row would otherwise be
    compared with its neighbour and look wrong)."""
    if len(a_lines) == len(b_lines):
        return list(range(len(a_lines)))
    from rapidfuzz import fuzz
    keys = [f.name for f in spec.high_stakes_line_fields if f.is_numeric]
    has_desc = any(f.name == "description" for f in spec.line_fields)

    def score(a: dict, b: dict) -> float:
        sc = sum(1.0 for k in keys if a[k].value is not None and
                 values_agree(a[k].spec.type, a[k].value, normalise(a[k].spec.type, (b.get(k) or {}).get("value"))))
        if has_desc and a["description"].value:
            sc += 2.0 * fuzz.token_set_ratio(str(a["description"].value), str((b.get("description") or {}).get("value") or "")) / 100
        return sc

    pairs = sorted(((score(a, b), i, j) for i, a in enumerate(a_lines) for j, b in enumerate(b_lines)), reverse=True)
    mapping: list[int | None] = [None] * len(a_lines)
    used: set[int] = set()
    for sc, i, j in pairs:
        if sc < 2.0 or mapping[i] is not None or j in used:
            continue
        mapping[i] = j
        used.add(j)
    return mapping


def apply_secondary(doc: ExtractedDoc, data: dict) -> list[FieldValue]:
    """Compare pass B with pass A. Returns the fields that disagree."""
    disagreements = []
    bf = data.get("fields", {})
    for f in doc.spec.fields:
        fa = doc.header[f.name]
        cell = bf.get(f.name) or {}
        bval = None if cell.get("legibility") == "not_present" else normalise(f.type, cell.get("value"))
        fa.double_read, fa.alt_value = True, bval
        fa.agreement = values_agree(f.type, fa.value, bval)
        if not fa.agreement:
            disagreements.append(fa)
    b_lines = data.get("line_items") or []
    if doc.spec.line_fields:
        doc.row_counts["primary"], doc.row_counts["secondary"] = len(doc.lines), len(b_lines)
        mapping = _align_lines(doc.lines, b_lines, doc.spec)
        for i, line in enumerate(doc.lines):
            j = mapping[i]
            for f in doc.spec.line_fields:
                fa = line[f.name]
                fa.double_read = True
                if j is None:
                    fa.alt_value, fa.agreement = None, (fa.value is None)
                else:
                    cell = b_lines[j].get(f.name) or {}
                    bval = None if cell.get("legibility") == "not_present" else normalise(f.type, cell.get("value"))
                    fa.alt_value = bval
                    fa.agreement = values_agree(f.type, fa.value, bval)
                if not fa.agreement:
                    disagreements.append(fa)
    return disagreements


# --- verifier ------------------------------------------------------------------------------------

def _crop(img: np.ndarray, bbox: dict) -> np.ndarray | None:
    h, w = img.shape[:2]
    bw, bh = bbox["x1"] - bbox["x0"], bbox["y1"] - bbox["y0"]
    padx, pady = max(0.06, bw * 0.6), max(0.03, bh * 2.0)
    x0, x1 = int(max(0, bbox["x0"] - padx) * w), int(min(1, bbox["x1"] + padx) * w)
    y0, y1 = int(max(0, bbox["y0"] - pady) * h), int(min(1, bbox["y1"] + pady) * h)
    if x1 - x0 < 10 or y1 - y0 < 10:
        return None
    crop = img[y0:y1, x0:x1]
    if crop.shape[1] < 900:
        import cv2
        s = 900 / crop.shape[1]
        crop = cv2.resize(crop, (900, int(crop.shape[0] * s)), interpolation=cv2.INTER_CUBIC)
    return crop


def run_verifier(llm: LLMClient, doc: ExtractedDoc, disputed: list[FieldValue], pages: list[PreparedPage],
                 model: str, long_edge: int) -> LLMResult:
    content: list[dict] = []
    for p in pages:
        content.append(text_block(f"Page {p.page_no} (full):"))
        content.append(_img(p.enhanced, long_edge))
    by_page = {p.page_no: p for p in pages}
    items = []
    crops = 0
    for fv in disputed[:40]:
        where = f"line {fv.line_index + 1}, column '{fv.spec.label}'" if fv.line_index is not None else fv.spec.label
        items.append(f'- key "{fv.key}" ({where}): A = "{fv.raw_text or fv.value or ""}" (normalised {fv.value!r}); '
                     f'B = normalised {fv.alt_value!r}')
        if fv.bbox and fv.page in by_page and crops < 12:
            c = _crop(by_page[fv.page].original, fv.bbox)
            if c is not None:
                content.append(text_block(f"Zoomed crop around {fv.key}:"))
                content.append(image_block(encode_jpeg(c, 92), "image/jpeg"))
                crops += 1
    content.append(text_block("Disputed fields:\n" + "\n".join(items) +
                              "\n\nReturn one verdict per disputed key, using the key strings exactly."))
    return llm.structured(pass_name="verify", model=model, system=prompts.VERIFY_SYSTEM, content=content,
                          schema=VERIFY_SCHEMA, max_tokens=16000)


def apply_verifier(doc: ExtractedDoc, data: dict) -> None:
    by_key = doc.by_key()
    for v in data.get("verdicts") or []:
        fv = by_key.get(v.get("key"))
        if fv is None:
            continue
        choice = v.get("choice")
        fv.verifier_choice, fv.verifier_certain = choice, bool(v.get("certain"))
        if choice == "B":
            fv.value, fv.alt_value = fv.alt_value, fv.value
            fv.raw_text = v.get("value_as_printed") or fv.raw_text
        elif choice == "neither":
            nv = normalise(fv.spec.type, v.get("value_as_printed"))
            fv.alt_value, fv.value = fv.value, nv
            fv.raw_text = v.get("value_as_printed") or ""
        elif choice == "unreadable":
            fv.legibility = "illegible"
        fv.raw_consistent = raw_consistent(fv.spec.type, fv.value, fv.raw_text)


# --- text layer grounding & location refinement ----------------------------------------------------

_NUM_TOKEN = re.compile(r"[\d०-९][\d०-९,]*(?:\.[\d०-९]+)?")


def ground_in_text_layer(doc: ExtractedDoc, pages: list[PreparedPage]) -> None:
    """For digital PDFs: is the extracted value literally present in the text layer?"""
    if not pages or not all(p.native_text for p in pages):
        return
    full = "\n".join(p.text for p in pages)
    numbers = {round(float(v), 2) for v in (parse_amount(t) for t in _NUM_TOKEN.findall(fold_digits(full))) if v is not None}
    codes = clean_code(full)
    squashed = re.sub(r"\s+", " ", full).lower()
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
