"""Per-document-type field specifications. One declarative spec drives: the strict JSON schema sent
to Claude, the second-pass (self-consistency) schema, normalisation, confidence rules, review UI
labels and export columns. Adding a field to a template is data, not code."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

LEGIBILITY = ["clear", "partly_legible", "illegible", "not_present"]
LANGS = ["english", "hindi", "marathi", "gujarati", "tamil", "telugu", "kannada", "bengali", "punjabi", "other"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    type: str = "string"  # string | text | number | integer | date | percent | gstin | pan | state | hsn | vehicle | ifsc
    group: str = "header"
    required: bool = False
    high_stakes: bool = False
    hint: str = ""

    @property
    def is_numeric(self) -> bool:
        return self.type in ("number", "percent", "integer")


@dataclass(frozen=True)
class DocSpec:
    doc_type: str
    label: str
    description: str
    fields: tuple[FieldSpec, ...]
    line_fields: tuple[FieldSpec, ...] = ()
    max_pages: int = 12
    extra_guidance: str = ""

    def all_field_specs(self) -> dict[str, FieldSpec]:
        d = {f.name: f for f in self.fields}
        d.update({f"line.{f.name}": f for f in self.line_fields})
        return d

    @property
    def high_stakes_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.high_stakes]

    @property
    def high_stakes_line_fields(self) -> list[FieldSpec]:
        return [f for f in self.line_fields if f.high_stakes]


F = FieldSpec

INVOICE = DocSpec(
    doc_type="invoice",
    label="Tax invoice",
    description="Indian GST tax invoice / bill of supply from a vendor",
    fields=(
        F("invoice_number", "Invoice number", required=True, high_stakes=True,
          hint="Also labelled Bill No., Inv. No., Invoice No."),
        F("invoice_date", "Invoice date", "date", required=True, high_stakes=True),
        F("copy_type", "Copy marked as", hint="e.g. ORIGINAL FOR RECIPIENT, DUPLICATE FOR TRANSPORTER"),
        F("supplier_name", "Vendor name", group="parties", required=True),
        F("supplier_gstin", "Vendor GSTIN", "gstin", group="parties", required=True, high_stakes=True),
        F("supplier_address", "Vendor address", "text", group="parties"),
        F("supplier_state", "Vendor state", "state", group="parties"),
        F("buyer_name", "Billed to", group="parties", required=True),
        F("buyer_gstin", "Buyer GSTIN", "gstin", group="parties", high_stakes=True),
        F("buyer_address", "Buyer address", "text", group="parties"),
        F("ship_to_address", "Ship-to address", "text", group="parties"),
        F("place_of_supply", "Place of supply", "state", group="header",
          hint="State name and/or 2-digit state code as printed"),
        F("po_reference", "PO number", hint="Buyer's order no. / PO no."),
        F("po_date", "PO date", "date"),
        F("eway_bill_number", "E-way bill no.", hint="12 digits"),
        F("irn", "IRN (e-invoice)", hint="64-character hash on e-invoices"),
        F("reverse_charge", "Reverse charge", hint="Yes/No as printed"),
        F("vehicle_number", "Vehicle no.", "vehicle"),
        F("due_date", "Due date", "date"),
        F("subtotal", "Taxable value", "number", group="amounts", required=True, high_stakes=True,
          hint="Total taxable value before GST"),
        F("discount_total", "Discount", "number", group="amounts", high_stakes=True),
        F("other_charges", "Freight / other charges", "number", group="amounts", high_stakes=True,
          hint="Freight, packing, insurance etc. added AFTER line items. If these are taxable and "
               "included in the taxable value, still report them here and say so in notes."),
        F("cgst_amount", "CGST", "number", group="amounts", high_stakes=True),
        F("sgst_amount", "SGST / UTGST", "number", group="amounts", high_stakes=True),
        F("igst_amount", "IGST", "number", group="amounts", high_stakes=True),
        F("cess_amount", "Cess", "number", group="amounts", high_stakes=True),
        F("tcs_amount", "TCS", "number", group="amounts", high_stakes=True),
        F("round_off", "Round off", "number", group="amounts", high_stakes=True,
          hint="Signed: negative if the total was rounded down"),
        F("grand_total", "Invoice total", "number", group="amounts", required=True, high_stakes=True),
        F("amount_in_words", "Amount in words", "text", group="amounts"),
        F("bank_account_number", "Bank account no.", group="payment"),
        F("bank_ifsc", "IFSC", "ifsc", group="payment"),
    ),
    line_fields=(
        F("description", "Description", "text", group="line_items", required=True),
        F("hsn_sac", "HSN/SAC", "hsn", group="line_items"),
        F("quantity", "Qty", "number", group="line_items", required=True, high_stakes=True),
        F("unit", "Unit", group="line_items", hint="e.g. NOS, KGS, MTR, PCS"),
        F("rate", "Rate", "number", group="line_items", high_stakes=True),
        F("rate_per", "Rate per", group="line_items",
          hint="The unit the rate applies to when printed in a 'per' column, e.g. NOS, 100 NOS, KG"),
        F("discount", "Discount", "number", group="line_items", high_stakes=True, hint="Amount, not %"),
        F("taxable_value", "Taxable value", "number", group="line_items", high_stakes=True),
        F("gst_rate", "GST %", "percent", group="line_items", high_stakes=True,
          hint="Combined rate, e.g. 18 for CGST 9% + SGST 9%"),
        F("cgst_amount", "CGST", "number", group="line_items", high_stakes=True),
        F("sgst_amount", "SGST", "number", group="line_items", high_stakes=True),
        F("igst_amount", "IGST", "number", group="line_items", high_stakes=True),
        F("line_total", "Line total", "number", group="line_items", high_stakes=True),
    ),
)

LR = DocSpec(
    doc_type="lr",
    label="Lorry receipt / GR / bilty",
    description="Transporter's consignment note (LR, GR, bilty, builty, challan)",
    fields=(
        F("lr_number", "LR / GR no.", required=True, high_stakes=True, hint="Also C/N No., GR No., Bilty No."),
        F("lr_date", "LR date", "date", required=True, high_stakes=True),
        F("transporter_name", "Transporter", group="parties", required=True),
        F("transporter_gstin", "Transporter GSTIN", "gstin", group="parties", high_stakes=True),
        F("vehicle_number", "Vehicle no.", "vehicle", required=True, high_stakes=True),
        F("from_location", "From", required=True),
        F("to_location", "To", required=True),
        F("consignor_name", "Consignor", group="parties", required=True),
        F("consignor_gstin", "Consignor GSTIN", "gstin", group="parties", high_stakes=True),
        F("consignee_name", "Consignee", group="parties", required=True),
        F("consignee_gstin", "Consignee GSTIN", "gstin", group="parties", high_stakes=True),
        F("invoice_reference", "Invoice no(s).", high_stakes=True),
        F("eway_bill_number", "E-way bill no.", high_stakes=True),
        F("goods_description", "Goods (said to contain)", "text"),
        F("packages_count", "No. of packages", "integer", group="amounts", required=True, high_stakes=True),
        F("package_type", "Packing", hint="e.g. boxes, bags, bundles, drums"),
        F("actual_weight_kg", "Actual weight (kg)", "number", group="amounts", high_stakes=True,
          hint="Convert tonnes/quintals to kg only in value; keep raw_text as printed"),
        F("charged_weight_kg", "Charged weight (kg)", "number", group="amounts", high_stakes=True),
        F("freight_rate", "Freight rate", "number", group="amounts", high_stakes=True),
        F("freight_amount", "Basic freight", "number", group="amounts", high_stakes=True),
        F("other_charges", "Other charges", "number", group="amounts", high_stakes=True,
          hint="Hamali, loading, door delivery, statistical charges etc. summed"),
        F("total_freight", "Total freight", "number", group="amounts", required=True, high_stakes=True),
        F("amount_in_words", "Amount in words", "text", group="amounts", hint="In English or Hindi, as written"),
        F("payment_mode", "Freight terms", hint="Paid / To pay / TBB (to be billed)"),
        F("gst_paid_by", "GST payable by", hint="Consignor / Consignee / Transporter"),
        F("declared_value", "Declared goods value", "number", group="amounts"),
    ),
    extra_guidance=("LRs are frequently carbon copies, partly handwritten, and bilingual (English + Hindi). "
                    "Read Hindi labels such as माल (goods), वजन (weight), भाड़ा (freight), कुल (total), "
                    "प्रेषक (consignor), प्रेषिती (consignee), गाड़ी नं. (vehicle no.). Devanagari numerals "
                    "(०१२३४५६७८९) must be converted to Western digits in `value` but kept verbatim in `raw_text`."),
)

PO = DocSpec(
    doc_type="po",
    label="Purchase order",
    description="Buyer's purchase order to a supplier",
    fields=(
        F("po_number", "PO number", required=True, high_stakes=True),
        F("po_date", "PO date", "date", required=True, high_stakes=True),
        F("buyer_name", "Buyer", group="parties", required=True),
        F("buyer_gstin", "Buyer GSTIN", "gstin", group="parties", high_stakes=True),
        F("supplier_name", "Supplier", group="parties", required=True),
        F("supplier_gstin", "Supplier GSTIN", "gstin", group="parties", high_stakes=True),
        F("delivery_date", "Delivery date", "date"),
        F("payment_terms", "Payment terms", "text"),
        F("subtotal", "Taxable value", "number", group="amounts", high_stakes=True),
        F("cgst_amount", "CGST", "number", group="amounts", high_stakes=True),
        F("sgst_amount", "SGST", "number", group="amounts", high_stakes=True),
        F("igst_amount", "IGST", "number", group="amounts", high_stakes=True),
        F("other_charges", "Other charges", "number", group="amounts", high_stakes=True),
        F("grand_total", "PO total", "number", group="amounts", required=True, high_stakes=True),
    ),
    line_fields=(
        F("item_code", "Item code", group="line_items"),
        F("description", "Description", "text", group="line_items", required=True),
        F("hsn_sac", "HSN/SAC", "hsn", group="line_items"),
        F("quantity", "Qty", "number", group="line_items", required=True, high_stakes=True),
        F("unit", "Unit", group="line_items"),
        F("rate", "Rate", "number", group="line_items", required=True, high_stakes=True),
        F("discount", "Discount", "number", group="line_items", high_stakes=True),
        F("gst_rate", "GST %", "percent", group="line_items", high_stakes=True),
        F("line_total", "Amount", "number", group="line_items", high_stakes=True,
          hint="Amount before tax as printed in the line"),
    ),
)

GRN = DocSpec(
    doc_type="grn",
    label="Goods receipt note",
    description="Goods receipt note / material inward note / MRN",
    fields=(
        F("grn_number", "GRN number", required=True, high_stakes=True),
        F("grn_date", "GRN date", "date", required=True, high_stakes=True),
        F("po_reference", "PO number", required=True, high_stakes=True),
        F("supplier_name", "Supplier", group="parties", required=True),
        F("supplier_gstin", "Supplier GSTIN", "gstin", group="parties", high_stakes=True),
        F("supplier_invoice_reference", "Supplier invoice no.", high_stakes=True),
        F("lr_reference", "LR / GR no."),
        F("vehicle_number", "Vehicle no.", "vehicle"),
        F("received_by", "Received by"),
    ),
    line_fields=(
        F("item_code", "Item code", group="line_items"),
        F("description", "Description", "text", group="line_items", required=True),
        F("unit", "Unit", group="line_items"),
        F("quantity_ordered", "Qty ordered", "number", group="line_items", high_stakes=True),
        F("quantity_received", "Qty received", "number", group="line_items", required=True, high_stakes=True),
        F("quantity_accepted", "Qty accepted", "number", group="line_items", high_stakes=True),
        F("quantity_rejected", "Qty rejected", "number", group="line_items", high_stakes=True),
        F("remarks", "Remarks", "text", group="line_items"),
    ),
)

CONTRACT = DocSpec(
    doc_type="contract",
    label="Contract",
    description="Commercial contract / rate contract / service agreement",
    max_pages=40,
    fields=(
        F("contract_title", "Title", "text", required=True),
        F("contract_number", "Contract / reference no."),
        F("party_a_name", "Party A", group="parties", required=True),
        F("party_b_name", "Party B", group="parties", required=True),
        F("effective_date", "Effective date", "date", required=True, high_stakes=True),
        F("expiry_date", "Expiry date", "date", high_stakes=True),
        F("auto_renewal", "Auto-renews?", hint="Yes / No / Not stated"),
        F("renewal_notice_days", "Renewal notice (days)", "integer", high_stakes=True),
        F("termination_notice_days", "Termination notice (days)", "integer", high_stakes=True),
        F("contract_value", "Contract value", "number", group="amounts", high_stakes=True),
        F("currency", "Currency"),
        F("payment_terms", "Payment terms", "text", high_stakes=True, hint="e.g. 45 days from invoice"),
        F("price_escalation", "Price escalation clause", "text", hint="One-sentence summary with clause no."),
        F("liability_cap", "Liability cap", "text", high_stakes=True),
        F("liquidated_damages", "Penalty / LD clause", "text", hint="Summary with clause no."),
        F("governing_law", "Governing law", "text"),
        F("jurisdiction", "Jurisdiction / arbitration seat", "text"),
        F("signatories", "Signed by", "text"),
    ),
    extra_guidance="For clause fields give a faithful one-sentence summary in `value` and the clause number in `raw_text`.",
)

SPECS: dict[str, DocSpec] = {s.doc_type: s for s in (INVOICE, LR, PO, GRN, CONTRACT)}
DOC_TYPES = list(SPECS)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def spec_with_extras(doc_type: str, extra_fields: list[dict] | None) -> DocSpec:
    """Template-defined extra fields (no code): [{name,label,type,high_stakes}]."""
    base = SPECS[doc_type]
    if not extra_fields:
        return base
    existing = {f.name for f in base.fields}
    extras = []
    for e in extra_fields:
        name = str(e.get("name", "")).strip().lower()
        if not _NAME_RE.match(name) or name in existing:
            continue
        t = e.get("type", "string")
        if t not in ("string", "text", "number", "integer", "date", "percent"):
            t = "string"
        extras.append(F(name, str(e.get("label") or name)[:80], t, group="custom",
                        high_stakes=bool(e.get("high_stakes"))))
    return DocSpec(base.doc_type, base.label, base.description, base.fields + tuple(extras),
                   base.line_fields, base.max_pages, base.extra_guidance)


# --- JSON schemas for structured outputs --------------------------------------------------------
# Union types (anyOf / nullable) are capped at 16 per request by the structured-outputs compiler,
# so schemas are union-free: every value is a string ("" when absent) that our own normaliser
# parses (Indian digit grouping, Devanagari numerals, day-first dates). page=0 / zero bbox mean
# "unknown location".

_BBOX = {"type": "object", "additionalProperties": False,
         "required": ["x0", "y0", "x1", "y1"],
         "properties": {k: {"type": "number"} for k in ("x0", "y0", "x1", "y1")}}

_CELL = {"type": "object", "additionalProperties": False, "required": ["value", "raw_text", "legibility"],
         "properties": {"value": {"type": "string"}, "raw_text": {"type": "string"},
                        "legibility": {"enum": LEGIBILITY}}}

_FIELD = {"type": "object", "additionalProperties": False,
          "required": ["value", "raw_text", "legibility", "page", "bbox"],
          "properties": {"value": {"type": "string"}, "raw_text": {"type": "string"},
                         "legibility": {"enum": LEGIBILITY}, "page": {"type": "integer"},
                         "bbox": {"$ref": "#/$defs/bbox"}}}


def primary_schema(spec: DocSpec) -> dict:
    header = {f.name: {"$ref": "#/$defs/field"} for f in spec.fields}
    schema: dict = {
        "type": "object", "additionalProperties": False,
        "$defs": {"bbox": _BBOX, "field": _FIELD, "cell": _CELL},
        "properties": {
            "document_type_observed": {"enum": DOC_TYPES + ["other"]},
            "languages": {"type": "array", "items": {"enum": LANGS}},
            "handwriting_present": {"type": "boolean"},
            "overall_legibility": {"enum": LEGIBILITY[:3]},
            "fields": {"type": "object", "additionalProperties": False,
                       "required": list(header), "properties": header},
            "anomalies": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["document_type_observed", "languages", "handwriting_present", "overall_legibility",
                     "fields", "anomalies"],
    }
    if spec.line_fields:
        cells = {f.name: {"$ref": "#/$defs/cell"} for f in spec.line_fields}
        schema["properties"]["line_items"] = {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["page", "row_bbox", "cells"],
                      "properties": {"page": {"type": "integer"}, "row_bbox": {"$ref": "#/$defs/bbox"},
                                     "cells": {"type": "object", "additionalProperties": False,
                                               "required": list(cells), "properties": cells}}}}
        schema["required"].append("line_items")
    return schema


def secondary_schema(spec: DocSpec) -> dict:
    """Independent full re-read (different framing, original pixels, no locations). Every field
    gets a second reading so agreement is evidence for all fields, not only the high-stakes ones."""
    header = {f.name: {"$ref": "#/$defs/cell"} for f in spec.fields}
    schema: dict = {
        "type": "object", "additionalProperties": False, "$defs": {"cell": _CELL},
        "properties": {"fields": {"type": "object", "additionalProperties": False,
                                  "required": list(header), "properties": header}},
        "required": ["fields"],
    }
    if spec.line_fields:
        cells = {f.name: {"$ref": "#/$defs/cell"} for f in spec.line_fields}
        schema["properties"]["line_items"] = {
            "type": "array", "items": {"type": "object", "additionalProperties": False,
                                       "required": list(cells), "properties": cells}}
        schema["required"].append("line_items")
    return schema


VERIFY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["key", "choice", "value_as_printed", "certain", "reason"],
        "properties": {
            "key": {"type": "string"},
            "choice": {"enum": ["A", "B", "neither", "unreadable"]},
            "value_as_printed": {"type": "string"},
            "certain": {"type": "boolean"},
            "reason": {"type": "string"},
        }}}},
}

TRIAGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["document_type", "pages", "languages", "handwriting_present", "is_business_document",
                 "issuer_gstin"],
    "properties": {
        "document_type": {"enum": DOC_TYPES + ["other"]},
        "issuer_gstin": {"type": "string"},
        "is_business_document": {"type": "boolean"},
        "languages": {"type": "array", "items": {"enum": LANGS}},
        "handwriting_present": {"type": "boolean"},
        "pages": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["page", "rotation_needed", "legible", "document_index"],
            "properties": {"page": {"type": "integer"},
                           "rotation_needed": {"enum": [0, 90, 180, 270]},
                           "legible": {"enum": LEGIBILITY[:3]},
                           "document_index": {"type": "integer"}}}},
    },
}
