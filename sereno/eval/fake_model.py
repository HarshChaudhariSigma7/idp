"""A deterministic model stand-in driven by synthetic ground truth, with controllable misreads.
Used by the test-suite and the offline demo seeder; never by production traffic."""
from __future__ import annotations

from datetime import date

from sereno.extraction.doc_specs import SPECS
from sereno.eval.synth import SynthDoc, inr


def _as_printed(name: str, ftype: str, v) -> str:
    if v is None:
        return ""
    if ftype == "date":
        return date.fromisoformat(v).strftime("%d/%m/%Y")
    if ftype in ("number",) and isinstance(v, float):
        return inr(v)
    return str(v)


def _value_str(ftype: str, v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}" if ftype == "number" else f"{v:g}"
    return str(v)


class TruthResponder:
    """errors: {"primary": {"grand_total": 99999.0, "line_items[0].quantity": 11.0}, "secondary": {...}}
    verifier_truthful: verifier returns the ground truth for disputed keys."""

    def __init__(self, sd: SynthDoc, errors: dict | None = None, legibility: dict | None = None,
                 verifier_truthful: bool = True, languages: list[str] | None = None, handwriting: bool = False,
                 overall_legibility: str = "clear", doc_index: dict | None = None, drop_lines: dict | None = None):
        self.sd = sd
        self.errors = errors or {}
        self.legibility = legibility or {}
        self.verifier_truthful = verifier_truthful
        self.languages = languages or (["english", "hindi"] if sd.doc_type == "lr" or sd.bucket == "bilingual" else ["english"])
        self.handwriting = handwriting
        self.overall_legibility = overall_legibility
        self.doc_index = doc_index  # triage: {page: document number} for batch-scan tests
        self.drop_lines = drop_lines or {}  # {"secondary": [1]}: that reading misses row 1

    def _truth(self, key: str):
        if key.startswith("line_items["):
            i = int(key[len("line_items["):key.index("]")])
            name = key.split(".", 1)[1]
            return self.sd.lines[i].get(name)
        return self.sd.truth.get(key)

    def _val(self, pass_name: str, key: str):
        errs = self.errors.get(pass_name, {})
        return errs[key] if key in errs else self._truth(key)

    def _cell(self, pass_name: str, key: str, ftype: str, with_loc: bool):
        v = self._val(pass_name, key)
        leg = self.legibility.get(key, "clear" if v is not None else "not_present")
        cell = {"value": _value_str(ftype, v), "raw_text": _as_printed(key, ftype, v), "legibility": leg}
        if with_loc:
            page, bb = self.sd.boxes.get(key, (0, None))
            cell["page"] = page if v is not None else 0
            cell["bbox"] = bb or {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
        return cell

    def __call__(self, pass_name: str, schema: dict, content: list) -> dict:
        spec = SPECS[self.sd.doc_type]
        if pass_name == "triage":
            return {"document_type": self.sd.doc_type, "is_business_document": True, "languages": self.languages,
                    "handwriting_present": self.handwriting,
                    "issuer_gstin": self.sd.truth.get("supplier_gstin") or self.sd.truth.get("transporter_gstin") or "",
                    "pages": [{"page": i, "rotation_needed": 0, "legible": self.overall_legibility,
                               "document_index": (self.doc_index or {}).get(i, 1)}
                              for i in range(1, max(1, sum(1 for b in content if b.get("type") == "image")) + 1)]}
        if pass_name == "primary":
            out = {"document_type_observed": self.sd.doc_type, "languages": self.languages,
                   "handwriting_present": self.handwriting, "overall_legibility": self.overall_legibility,
                   "anomalies": [],
                   "fields": {f.name: self._cell("primary", f.name, f.type, True) for f in spec.fields}}
            if spec.line_fields:
                out["line_items"] = []
                for i in range(len(self.sd.lines)):
                    page, bb = self.sd.boxes.get(f"line_items[{i}]", (1, None))
                    out["line_items"].append({"page": page, "row_bbox": bb or {"x0": 0, "y0": 0, "x1": 0, "y1": 0},
                                              "cells": {f.name: self._cell("primary", f"line_items[{i}].{f.name}", f.type, False)
                                                        for f in spec.line_fields}})
            return out
        if pass_name == "secondary":
            out = {"fields": {f.name: self._cell("secondary", f.name, f.type, False) for f in spec.fields}}
            if spec.line_fields:
                out["line_items"] = [{f.name: self._cell("secondary", f"line_items[{i}].{f.name}", f.type, False)
                                      for f in spec.line_fields} for i in range(len(self.sd.lines))
                                     if i not in self.drop_lines.get("secondary", [])]
            return out
        if pass_name == "crop":
            import re as _re
            reads = []
            for b in content:
                m = _re.match(r'Crop (\d+) \[key "([^"]+)"\]', b.get("text", "")) if b.get("type") == "text" else None
                if not m:
                    continue
                key = m.group(2)
                name = key.split(".", 1)[1] if key.startswith("line_items[") else key
                ftype = (spec.all_field_specs().get(f"line.{name}") if key.startswith("line_items[") else
                         spec.all_field_specs().get(name))
                ftype = ftype.type if ftype else "string"
                v = self._val("crop", key)
                leg = self.legibility.get(f"crop:{key}", "clear" if v is not None else "not_present")
                reads.append({"crop": int(m.group(1)), "value": _value_str(ftype, v),
                              "raw_text": _as_printed(key, ftype, v), "legibility": leg})
            return {"reads": reads}
        if pass_name == "verify":
            text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            verdicts = []
            for line in text.splitlines():
                if not line.startswith('- key "'):
                    continue
                key = line.split('"')[1]
                t = self._truth(key)
                a, b = self._val("primary", key), self._val("secondary", key)
                if not self.verifier_truthful:
                    choice = "A"
                elif a == t:
                    choice = "A"
                elif b == t:
                    choice = "B"
                else:
                    choice = "neither"
                verdicts.append({"key": key, "choice": choice, "value_as_printed": _value_str("number", t) if isinstance(t, float) else str(t or ""),
                                 "certain": True, "reason": "read from crop"})
            return {"verdicts": verdicts}
        raise ValueError(pass_name)
