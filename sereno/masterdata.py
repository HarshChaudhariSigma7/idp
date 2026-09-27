"""Company master data that makes every document easier to read correctly:

* the customer's own GSTINs (admin-entered, plus any buyer GSTIN confirmed on 3+ reviewed invoices),
* a profile per vendor learned from reviewed documents (name, bank account, invoice-number format),
* duplicate detection on (issuer GSTIN, document number), stored only as a keyed hash,
* vendor templates that switch on by themselves once a vendor has 3 reviewed documents.
"""
from __future__ import annotations

from sqlalchemy import select

from sereno.extraction.normalize import clean_code, shape_mask
from sereno.extraction.result import ExtractedDoc
from sereno.models import Document, Template, Tenant, VendorProfile, utcnow
from sereno.review import effective_value
from sereno.security import audit
from sereno.security.crypto import pseudonymise
from sereno.validation.context import CheckContext, VendorSnapshot

ISSUER = {"invoice": ("supplier_gstin", "invoice_number"), "lr": ("transporter_gstin", "lr_number"),
          "po": ("buyer_gstin", "po_number"), "grn": ("supplier_gstin", "grn_number")}
LEARN_OWN_AFTER = 3
AUTO_TEMPLATE_AFTER = 3


def own_gstins(tenant: Tenant) -> set[str]:
    st = tenant.settings or {}
    explicit = {clean_code(g) for g in st.get("own_gstins", []) if g}
    learned = {g for g, n in (st.get("buyer_gstin_counts") or {}).items() if n >= LEARN_OWN_AFTER}
    return explicit | learned


def dedupe_key(doc_type: str, values: dict) -> str | None:
    pair = ISSUER.get(doc_type)
    if not pair:
        return None
    issuer, number = values.get(pair[0]), values.get(pair[1])
    if not issuer or not number:
        return None
    return pseudonymise(f"{doc_type}|{clean_code(issuer)}|{clean_code(number)}")


def build_context(s, tenant: Tenant, doc_x: ExtractedDoc, document_id: str) -> tuple[CheckContext, str | None]:
    ctx = CheckContext(own_gstins=own_gstins(tenant))
    dt = doc_x.spec.doc_type
    vendor_gstin = doc_x.v("supplier_gstin") if dt == "invoice" else None
    if vendor_gstin:
        prof = s.execute(select(VendorProfile).where(VendorProfile.tenant_id == tenant.id,
                                                     VendorProfile.vendor_key == pseudonymise(vendor_gstin))).scalar_one_or_none()
        if prof is not None:
            ctx.vendor = VendorSnapshot(prof.name, prof.bank_account, prof.invoice_masks or {}, prof.docs)
    key = dedupe_key(dt, {f.name: doc_x.v(f.name) for f in doc_x.spec.fields})
    if key:
        for other in s.execute(select(Document).where(Document.tenant_id == tenant.id, Document.dedupe_key == key,
                                                      Document.id != document_id, Document.status != "failed")).scalars():
            ctx.duplicates.append({"document_id": other.id, "created_at": other.created_at,
                                   "match": "same issuer and document number"})
    return ctx, key


def learn(s, doc: Document) -> None:
    """Update company/vendor knowledge from a finished document. Bank details are learned only
    from documents a person has reviewed; names and number formats also from auto-approved ones."""
    if doc.doc_type != "invoice":
        return
    vals = {f.field_name: effective_value(f) for f in doc.fields if f.line_index is None}
    tenant = s.get(Tenant, doc.tenant_id)
    reviewed = doc.reviewed_at is not None
    if reviewed and vals.get("buyer_gstin"):
        st = dict(tenant.settings or {})
        counts = dict(st.get("buyer_gstin_counts") or {})
        counts[vals["buyer_gstin"]] = counts.get(vals["buyer_gstin"], 0) + 1
        st["buyer_gstin_counts"] = counts
        tenant.settings = st
    g = vals.get("supplier_gstin")
    if not g:
        return
    vkey = pseudonymise(g)
    prof = s.execute(select(VendorProfile).where(VendorProfile.tenant_id == doc.tenant_id,
                                                 VendorProfile.vendor_key == vkey)).scalar_one_or_none()
    if prof is None:
        prof = VendorProfile(tenant_id=doc.tenant_id, vendor_key=vkey, invoice_masks={}, docs=0)
        s.add(prof)
    prof.docs = (prof.docs or 0) + 1
    if vals.get("supplier_name") and (reviewed or not prof.name):
        prof.name = vals["supplier_name"]
    if vals.get("invoice_number"):
        masks = dict(prof.invoice_masks or {})
        m = shape_mask(vals["invoice_number"])
        masks[m] = masks.get(m, 0) + 1
        prof.invoice_masks = masks
    if reviewed and vals.get("bank_account_number"):
        prof.bank_account = clean_code(vals["bank_account_number"])
    prof.updated_at = utcnow()
    s.flush()
    if reviewed:
        _maybe_auto_template(s, doc, vkey, vals.get("supplier_name"))


def _maybe_auto_template(s, doc: Document, vkey: str, name: str | None) -> None:
    from sereno.templates_onboarding import build_hints
    active = s.execute(select(Template).where(Template.tenant_id == doc.tenant_id, Template.vendor_key == vkey,
                                              Template.doc_type == doc.doc_type, Template.status == "active")).scalars().first()
    examples = s.execute(select(Document).where(Document.tenant_id == doc.tenant_id, Document.vendor_key == vkey,
                                                Document.doc_type == doc.doc_type, Document.reviewed_at.is_not(None))
                         .order_by(Document.reviewed_at.desc()).limit(5)).scalars().all()
    if len(examples) < AUTO_TEMPLATE_AFTER:
        return
    hints = build_hints(examples)
    if active is not None:
        if (active.name or "").startswith("Auto:") and len(examples) > len(active.example_document_ids or []):
            active.hints, active.example_document_ids = hints, [d.id for d in examples]
        return
    t = Template(tenant_id=doc.tenant_id, doc_type=doc.doc_type, name=f"Auto: {name or 'vendor'}"[:200], vendor_key=vkey,
                 hints=hints, extra_fields=[], example_document_ids=[d.id for d in examples], status="active")
    s.add(t)
    s.flush()
    audit.record("template.auto_created", session=s, tenant_id=doc.tenant_id, object_type="template", object_id=t.id,
                 examples=len(examples))
