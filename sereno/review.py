"""Human-in-the-loop: field-level review actions. Every action is logged against the field and the
extraction that produced it; completion feeds historical vendor accuracy (confidence signal d)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from sereno.config import get_settings
from sereno.extraction.doc_specs import SPECS, spec_with_extras
from sereno.extraction.normalize import normalise
from sereno.extraction.result import ExtractedDoc, FieldValue
from sereno.models import Correction, Document, ExtractedField, Template, ValidationResult, VendorFieldStat, utcnow
from sereno.security import audit
from sereno.storage.temp_store import DOCS, get_store
from sereno.validation.checks import run_checks


class ReviewError(ValueError):
    pass


def effective_value(f: ExtractedField):
    return f.final_value if f.status == "corrected" else f.value


def act_on_field(s, user, doc: Document, field: ExtractedField, action: str, value=None, time_ms: int | None = None) -> ExtractedField:
    if field.document_id != doc.id:
        raise ReviewError("field does not belong to document")
    if doc.reviewed_at is not None:
        raise ReviewError("This document has already been completed")
    old = effective_value(field)
    if action == "confirm":
        if field.value is None and field.status != "corrected" and field.reason_codes and "manual_entry" in field.reason_codes:
            pass  # confirming a blank is allowed: field genuinely absent
        field.status = "confirmed" if field.status != "corrected" else "corrected"
        new = old
    elif action == "correct":
        new = normalise(field.value_type, None if value is None else str(value))
        if value not in (None, "") and new is None:
            raise ReviewError(f"'{value}' isn't a valid {_type_word(field.value_type)}")
        field.final_value = new
        field.status = "corrected"
    else:
        raise ReviewError("unknown action")
    was_flagged = field.needs_review
    s.add(Correction(field_id=field.id, document_id=doc.id, user_id=user.id, action=action,
                     old_value=old, new_value=new, was_flagged=was_flagged, score_at_review=field.score,
                     time_on_field_ms=time_ms))
    s.flush()
    audit.record(f"review.field_{action}", session=s, tenant_id=doc.tenant_id, actor_id=user.id, object_type="field",
                 object_id=field.id, document_id=doc.id, was_flagged=was_flagged,
                 changed=bool(action == "correct" and not _same(old, new)), ms=time_ms)
    return field


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 0.005
        except (TypeError, ValueError):
            return False
    return a == b


def _type_word(t: str) -> str:
    return {"number": "amount", "percent": "percentage", "integer": "whole number", "date": "date (DD/MM/YYYY)"}.get(t, "value")


def rebuild_extracted(s, doc: Document) -> ExtractedDoc:
    """Current effective values (after corrections) in ExtractedDoc form, for re-validation."""
    extras = None
    if doc.template_id:
        t = s.get(Template, doc.template_id)
        extras = t.extra_fields if t else None
    spec = spec_with_extras(doc.doc_type, extras) if doc.doc_type in SPECS else None
    if spec is None:
        raise ReviewError("unknown document type")
    specs = spec.all_field_specs()
    header, lines = {}, {}
    for f in doc.fields:
        fs = specs.get(f"line.{f.field_name}" if f.line_index is not None else f.field_name)
        if fs is None:
            continue
        fv = FieldValue(key=f.key, spec=fs, line_index=f.line_index, value=effective_value(f))
        if f.line_index is None:
            header[f.field_name] = fv
        else:
            lines.setdefault(f.line_index, {})[f.field_name] = fv
    return ExtractedDoc(spec=spec, header=header, lines=[lines[i] for i in sorted(lines)])


def recheck(s, doc: Document) -> list[dict]:
    st = get_settings()
    checks = run_checks(rebuild_extracted(s, doc), st.amount_tolerance_abs, st.amount_tolerance_rel)
    return [{"check_id": c.check_id, "message": c.message, "severity": c.severity, "field_keys": c.field_keys}
            for c in checks if c.failed]


def complete_document(s, user, doc: Document, override_failed_checks: bool = False) -> dict:
    pending = [f for f in doc.fields if f.needs_review and f.status == "pending"]
    if pending:
        raise ReviewError(f"{len(pending)} flagged field(s) still need a decision")
    failed = recheck(s, doc)
    blocking = [c for c in failed if c["severity"] == "error"]
    if blocking and not override_failed_checks:
        return {"completed": False, "failed_checks": failed}
    st = get_settings()
    now = utcnow()
    for c in run_checks(rebuild_extracted(s, doc), st.amount_tolerance_abs, st.amount_tolerance_rel):
        s.add(ValidationResult(document_id=doc.id, check_id=c.check_id, status=c.status, severity=c.severity,
                               message=c.message, field_keys=c.field_keys, stage="final"))
    doc.reviewed_at = now
    if doc.status in ("needs_review", "unreadable"):
        doc.status = "ready"
        doc.status_message = ("Ready to export — reviewed and confirmed" if not blocking else
                              "Ready to export — reviewer confirmed figures as printed (they don't fully reconcile)")
    doc.blob_expires_at = min(doc.blob_expires_at or now, now + timedelta(hours=st.doc_grace_after_done_hours))
    get_store().set_expiry(DOCS, doc.id, doc.blob_expires_at)
    _update_vendor_stats(s, doc)
    corrected = sum(1 for f in doc.fields if f.status == "corrected")
    audit.record("review.document_completed", session=s, tenant_id=doc.tenant_id, actor_id=user.id, object_type="document",
                 object_id=doc.id, corrected=corrected, overridden=bool(blocking), qa_sample=doc.is_qa_sample,
                 turnaround_s=int((now - doc.review_entered_at).total_seconds()) if doc.review_entered_at else None)
    return {"completed": True, "failed_checks": failed}


def _update_vendor_stats(s, doc: Document) -> None:
    if not doc.vendor_key or not doc.doc_type:
        return
    for f in doc.fields:
        # A field counts as evidence only if a human actually looked at it.
        looked = f.status in ("confirmed", "corrected") or doc.is_qa_sample
        if not looked:
            continue
        name = f"line.{f.field_name}" if f.line_index is not None else f.field_name
        row = s.execute(select(VendorFieldStat).where(VendorFieldStat.tenant_id == doc.tenant_id,
                                                      VendorFieldStat.vendor_key == doc.vendor_key,
                                                      VendorFieldStat.doc_type == doc.doc_type,
                                                      VendorFieldStat.field_name == name)).scalar_one_or_none()
        if row is None:
            row = VendorFieldStat(tenant_id=doc.tenant_id, vendor_key=doc.vendor_key, doc_type=doc.doc_type,
                                  field_name=name, correct=0, total=0)
            s.add(row)
        row.total += 1
        if f.status != "corrected" or _same(f.final_value, f.value):
            row.correct += 1
        s.flush()
