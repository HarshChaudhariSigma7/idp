"""In-memory representation of one document's extraction, shared by validation, scoring and
persistence."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sereno.extraction.doc_specs import DocSpec, FieldSpec


@dataclass
class FieldValue:
    key: str
    spec: FieldSpec
    line_index: int | None
    value: Any
    raw_text: str = ""
    legibility: str = "not_present"
    page: int | None = None
    bbox: dict | None = None
    # --- signals filled by the pipeline ---
    double_read: bool = False           # was this field re-read by the second pass?
    alt_value: Any = None               # second-pass value
    agreement: bool | None = None       # A == B ?
    verifier_choice: str | None = None  # A | B | neither | unreadable
    verifier_certain: bool | None = None
    raw_consistent: bool | None = None  # does raw_text parse to value?
    in_text_layer: bool | None = None   # digital PDFs: value literally present in the text layer
    format_ok: bool | None = None       # checksum / regex validity
    unparseable: bool = False           # model gave text we could not parse into the field type
    # --- machine-readable codes (e-invoice QR, UPI QR, barcodes) ---
    code_value: Any = None
    code_source: str | None = None      # einvoice_qr | upi_qr | barcode
    code_agrees: bool | None = None
    code_filled: bool = False           # value came from the code because nothing legible was printed
    # --- third blind read (zoomed crops) and voting ---
    third_value: Any = None
    votes: str | None = None            # e.g. "3/3", "2/3", "1/1/1"
    # --- arithmetic-guided repair / master data ---
    suggested_value: Any = None
    suggestion_reason: str | None = None
    repaired: bool = False              # value was changed by arithmetic + zoomed re-read evidence
    master_match: bool | None = None    # matches the company's own records / vendor master
    evidence: list = field(default_factory=list)  # short plain tags shown to reviewers

    @property
    def present(self) -> bool:
        return self.value is not None


@dataclass
class ExtractedDoc:
    spec: DocSpec
    header: dict[str, FieldValue]
    lines: list[dict[str, FieldValue]] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    handwriting: bool = False
    legibility: str = "clear"
    anomalies: list[str] = field(default_factory=list)
    observed_type: str | None = None
    codes: list = field(default_factory=list)          # CodeReading objects found on the pages
    row_counts: dict = field(default_factory=dict)     # {"primary": n, "secondary": m, "qr": k}

    def v(self, name: str):
        f = self.header.get(name)
        return f.value if f else None

    def all_fields(self) -> list[FieldValue]:
        out = list(self.header.values())
        for line in self.lines:
            out.extend(line.values())
        return out

    def by_key(self) -> dict[str, FieldValue]:
        return {f.key: f for f in self.all_fields()}


def line_key(i: int, name: str) -> str:
    return f"line_items[{i}].{name}"
