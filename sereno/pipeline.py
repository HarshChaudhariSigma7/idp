"""End-to-end document processing: ingest -> pre-check -> correct -> triage -> route model ->
pass A -> pass B -> verify -> validate -> score -> route fields -> persist -> audit."""
from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from sereno import jobs
from sereno.config import get_settings
from sereno.confidence.scorer import score_document
from sereno.db import session_scope
from sereno.extraction import extractor
from sereno.extraction.doc_specs import SPECS, TRIAGE_SCHEMA, spec_with_extras
from sereno.extraction.llm import LLMClient, LLMError, LLMRefusal, LLMResult, get_llm, image_block, text_block
from sereno.extraction.prompts import PROMPT_VERSION, TRIAGE_SYSTEM
from sereno.extraction.result import ExtractedDoc
from sereno.ingest import loader, preprocess, quality
from sereno.models import (Document, ExtractedField, ExtractionRun, Page, Template, Tenant, ThresholdConfig, User,
                           ValidationResult, VendorFieldStat, utcnow)
from sereno.routing import ready_message, review_headline
from sereno.security import audit
from sereno.security.crypto import pseudonymise
from sereno.storage.temp_store import DOCS, EVAL, BlobGone, get_store
from sereno.validation.checks import CheckResult, run_checks

log = logging.getLogger("sereno.pipeline")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
VENDOR_FIELD = {"invoice": "supplier_gstin", "lr": "transporter_gstin", "po": "supplier_gstin",
                "grn": "supplier_gstin", "contract": None}


# --- upload --------------------------------------------------------------------------------------

@dataclass
class UploadOutcome:
    document_id: str
    duplicate: bool


def ingest_upload(s, tenant_id: str, user_id: str, filename: str, data: bytes, declared_type: str = "auto") -> UploadOutcome:
    st = get_settings()
    mime = loader.sniff_mime(data, filename)  # raises UnsupportedDocument with a plain message
    if len(data) > 40 * 1024 * 1024:
        raise loader.UnsupportedDocument("File is larger than 40 MB. Please split it or scan at 300 DPI.")
    sha = hashlib.sha256(data).hexdigest()
    existing = s.execute(select(Document).where(Document.tenant_id == tenant_id, Document.content_sha256 == sha,
                                                Document.blob_purged_at.is_(None))).scalars().first()
    if existing:
        audit.record("document.upload_duplicate", session=s, tenant_id=tenant_id, actor_id=user_id,
                     object_type="document", object_id=existing.id)
        return UploadOutcome(existing.id, True)
    if declared_type not in SPECS and declared_type != "auto":
        declared_type = "auto"
    doc = Document(tenant_id=tenant_id, uploaded_by=user_id, filename=filename[:255], content_sha256=sha, mime_type=mime,
                   declared_type=declared_type, status="uploaded",
                   blob_expires_at=utcnow() + timedelta(hours=st.doc_ttl_hours))
    s.add(doc)
    s.flush()
    get_store().put(DOCS, doc.id, "source", data, doc.blob_expires_at)
    jobs.enqueue(s, "process_document", {"document_id": doc.id})
    audit.record("document.uploaded", session=s, tenant_id=tenant_id, actor_id=user_id, object_type="document",
                 object_id=doc.id, bytes=len(data), mime=mime.replace("/", "_"), declared_type=declared_type)
    return UploadOutcome(doc.id, False)


# --- processing ----------------------------------------------------------------------------------

def _log_run(s, doc_id: str, pass_name: str, res: LLMResult | None, model: str, error: str | None = None) -> None:
    s.add(ExtractionRun(document_id=doc_id, pass_name=pass_name, model=model,
                        served_model=res.served_model if res else None, prompt_version=PROMPT_VERSION,
                        request_id=res.request_id if res else None,
                        input_tokens=res.input_tokens if res else 0, output_tokens=res.output_tokens if res else 0,
                        latency_ms=res.latency_ms if res else 0, stop_reason=res.stop_reason if res else None,
                        output=res.data if res else None, error=error))


def _thresholds(s, doc_type: str, tenant: Tenant) -> tuple[float, float, dict | None]:
    st = get_settings()
    auto, hs, weights = st.auto_accept_threshold, st.high_stakes_threshold, None
    tc = s.execute(select(ThresholdConfig).where(ThresholdConfig.doc_type == doc_type, ThresholdConfig.active.is_(True))
                   .order_by(ThresholdConfig.created_at.desc())).scalars().first()
    if tc:
        auto, hs, weights = tc.auto_accept, tc.high_stakes, tc.weights
    ov = (tenant.settings or {}).get("thresholds", {}).get(doc_type, {})
    # Tenants can only make thresholds stricter, never looser.
    return max(auto, ov.get("auto_accept", 0)), max(hs, ov.get("high_stakes", 0)), weights


def _history(s, tenant_id: str, vendor_key: str | None, doc_type: str) -> dict[str, tuple[int, int]]:
    if not vendor_key:
        return {}
    rows = s.execute(select(VendorFieldStat).where(VendorFieldStat.tenant_id == tenant_id,
                                                   VendorFieldStat.vendor_key == vendor_key,
                                                   VendorFieldStat.doc_type == doc_type)).scalars()
    return {r.field_name: (r.correct, r.total) for r in rows}


def _pick_reviewer(s, doc: Document, tenant: Tenant) -> str | None:
    open_load = (select(Document.assigned_to, func.count().label("n"))
                 .where(Document.status.in_(["needs_review", "unreadable"]), Document.reviewed_at.is_(None))
                 .group_by(Document.assigned_to).subquery())
    if tenant.managed_review:
        cand = select(User.id).where(User.role == "sereno_reviewer", User.tenant_id.is_(None), User.is_active.is_(True))
    else:
        cand = select(User.id).where(User.tenant_id == tenant.id, User.role == "reviewer", User.is_active.is_(True))
    q = (select(User.id, func.coalesce(open_load.c.n, 0)).where(User.id.in_(cand))
         .outerjoin(open_load, open_load.c.assigned_to == User.id).order_by(func.coalesce(open_load.c.n, 0), User.created_at))
    row = s.execute(q).first()
    if row:
        return row[0]
    fallback = s.execute(select(User.id).where(User.tenant_id == tenant.id, User.role.in_(["manager", "admin"]),
                                               User.is_active.is_(True)).order_by(User.created_at)).first()
    return fallback[0] if fallback else None


def _enter_review(s, doc: Document, tenant: Tenant) -> None:
    st = get_settings()
    doc.review_entered_at = utcnow()
    doc.review_due_at = doc.review_entered_at + timedelta(minutes=st.review_sla_minutes)
    doc.assigned_to = _pick_reviewer(s, doc, tenant)
    # keep the source long enough to review, never beyond the hard cap
    cap = doc.created_at + timedelta(hours=st.doc_hard_cap_hours)
    doc.blob_expires_at = min(cap, max(doc.blob_expires_at or cap, doc.review_due_at + timedelta(hours=48)))
    get_store().set_expiry(DOCS, doc.id, doc.blob_expires_at)


def _triage(llm: LLMClient, pages: list[loader.LoadedPage], model: str) -> LLMResult:
    content = []
    for p in pages[:6]:
        content.append(text_block(f"Page {p.page_no}:"))
        content.append(image_block(loader.encode_jpeg(preprocess.fit_long_edge(p.image, 1200), 85), "image/jpeg"))
    content.append(text_block("Triage this document."))
    return llm.structured(pass_name="triage", model=model, system=TRIAGE_SYSTEM, content=content,
                          schema=TRIAGE_SCHEMA, max_tokens=4000, effort="low")


def process_document(document_id: str, llm: LLMClient | None = None) -> None:
    st = get_settings()
    llm = llm or get_llm()
    store = get_store()
    t0 = time.monotonic()
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None or doc.status not in ("uploaded", "processing", "failed"):
            return
        doc.status, doc.status_message = "processing", "Reading document…"
        tenant_id, declared = doc.tenant_id, doc.declared_type
        # idempotent on retry: derived rows are rebuilt; extraction_runs are kept (they are the log)
        for model in (Page, ExtractedField, ValidationResult):
            s.query(model).filter(model.document_id == document_id).delete(synchronize_session=False)
    audit.record("document.processing_started", tenant_id=tenant_id, object_type="document", object_id=document_id)

    try:
        raw = store.get(DOCS, document_id, "source")
    except BlobGone:
        _finish_failed(document_id, "The original file has already been deleted under the retention policy. Please upload it again.")
        return

    try:
        loaded = loader.load(raw, max_pages=40, render_dpi=200)
    except loader.UnsupportedDocument as e:
        _finish_failed(document_id, str(e))
        raise jobs.PermanentJobError(str(e))

    cost = 0.0
    runs: list[tuple[str, LLMResult | None, str, str | None]] = []

    # --- triage (type, rotation, languages, handwriting, issuer) --------------------------------
    triage = None
    need_triage = declared == "auto" or not loaded.is_digital
    if need_triage:
        try:
            tr = _triage(llm, loaded.pages, st.model_triage)
            triage, cost = tr.data, cost + tr.cost_usd
            runs.append(("triage", tr, st.model_triage, None))
        except LLMError as e:
            runs.append(("triage", None, st.model_triage, str(e)))
    doc_type = declared if declared in SPECS else (triage or {}).get("document_type", "other")
    if doc_type not in SPECS:
        _persist_runs(document_id, runs)
        _finish_needs_human(document_id, doc_type=None,
                            message="Needs your review — this doesn't look like an invoice, LR, PO, GRN or contract")
        return
    spec_base = SPECS[doc_type]
    pages_in = loaded.pages[:spec_base.max_pages]
    rotations = {p.get("page"): p.get("rotation_needed", 0) for p in (triage or {}).get("pages", [])}

    # --- pre-check + auto-correct ---------------------------------------------------------------
    prepared: list[extractor.PreparedPage] = []
    page_rows = []
    page_buckets, unreadable = [], []
    for p in pages_in:
        img = preprocess.rotate_quarter(p.image, int(rotations.get(p.page_no, 0) or 0))
        q0 = quality.assess(img, p.source_dpi, p.native_text)
        enhanced, applied = preprocess.enhance(img, q0)
        if rotations.get(p.page_no):
            applied.insert(0, f"rotate:{rotations[p.page_no]}")
        original = preprocess.deskew(img, q0.skew_deg) if any(a.startswith("deskew:+") or a.startswith("deskew:-") for a in applied) else img
        q1 = quality.assess(enhanced, p.source_dpi, p.native_text) if applied else q0
        # Gate on the ORIGINAL pixels: enhancement can make a page look sharper without adding information.
        reason = quality.unreadable_reason(q0, st.blur_floor, st.contrast_floor, st.min_char_height_px, st.min_ink_ratio)
        b = "unreadable" if reason else quality.bucket(q0)
        page_buckets.append(b)
        if reason:
            unreadable.append((p.page_no, reason))
        prepared.append(extractor.PreparedPage(p.page_no, enhanced, original, p.native_text, p.text, p.words))
        page_rows.append((p.page_no, enhanced, {"before": q0.as_dict(), "after": q1.as_dict(), "bucket": b}, applied))

    with session_scope() as s:
        doc = s.get(Document, document_id)
        for page_no, img, qd, applied in page_rows:
            store.put(DOCS, document_id, f"page-{page_no}", loader.encode_jpeg(preprocess.fit_long_edge(img, 2600), 88),
                      doc.blob_expires_at)
            s.add(Page(document_id=document_id, page_no=page_no, width=img.shape[1], height=img.shape[0],
                       quality=qd, corrections_applied=applied))
        doc.page_count = len(page_rows)
        doc.is_digital = loaded.is_digital
        doc.doc_type = doc_type
        doc.quality_bucket = quality.document_bucket(page_buckets)
        doc.quality = {"pages": [r[2]["before"] for r in page_rows],
                       "corrections": sorted({a.split(":")[0] for r in page_rows for a in r[3]}),
                       "truncated_pages": max(0, len(loaded.pages) - len(pages_in))}
        langs = set((triage or {}).get("languages") or [])
        if loaded.is_digital:
            langs.add("english")
            if any(_DEVANAGARI.search(p.text or "") for p in pages_in):
                langs.add("hindi")
        doc.languages = sorted(langs) or None
        doc.has_handwriting = bool((triage or {}).get("handwriting_present"))
        if triage and triage.get("issuer_gstin"):
            doc.vendor_key = pseudonymise(triage["issuer_gstin"])
        tenant = s.get(Tenant, doc.tenant_id)
        template = None
        if doc.vendor_key:
            template = s.execute(select(Template).where(Template.tenant_id == tenant.id, Template.vendor_key == doc.vendor_key,
                                                        Template.doc_type == doc_type, Template.status == "active")
                                 .order_by(Template.created_at.desc())).scalars().first()
        template_hints = template.hints if template else None
        extra_fields = template.extra_fields if template else None
        doc.template_id = template.id if template else None
        bucket = doc.quality_bucket

    audit.record("document.precheck", tenant_id=tenant_id, object_type="document", object_id=document_id,
                 bucket=bucket, pages=len(page_rows), unreadable_pages=len(unreadable))

    if unreadable and len(unreadable) == len(prepared):
        _persist_runs(document_id, runs)
        _finish_unreadable(document_id, doc_type, unreadable[0][1], cost, t0)
        return

    spec = spec_with_extras(doc_type, extra_fields)
    langs = set((triage or {}).get("languages") or [])
    clean = (bucket == "digital" and langs <= {"english"} and not (triage or {}).get("handwriting_present"))
    model = st.model_clean if clean else st.model_complex

    def attempt(model_name: str) -> tuple[ExtractedDoc, list[CheckResult], int]:
        nonlocal cost
        a = extractor.run_primary(llm, spec, prepared, model_name, st.max_image_long_edge, template_hints)
        runs.append(("primary", a, model_name, None))
        cost += a.cost_usd
        d = extractor.parse_primary(spec, a.data)
        b = extractor.run_secondary(llm, spec, prepared, model_name, st.max_image_long_edge)
        runs.append(("secondary", b, model_name, None))
        cost += b.cost_usd
        disputed = extractor.apply_secondary(d, b.data)
        extractor.ground_in_text_layer(d, prepared)
        chk = run_checks(d, st.amount_tolerance_abs, st.amount_tolerance_rel)
        return d, chk, len(disputed)

    try:
        doc_x, checks, n_disputed = attempt(model)
        errors = [c for c in checks if c.failed and c.severity == "error"]
        if model != st.model_complex and (n_disputed or errors):
            # Escalate: the cheaper model is only trusted when everything reconciles.
            doc_x, checks, n_disputed = attempt(st.model_complex)
            model = st.model_complex
        disputed = [f for f in doc_x.all_fields() if f.double_read and f.agreement is False]
        if disputed:
            v = extractor.run_verifier(llm, doc_x, disputed, prepared, st.model_complex, st.max_image_long_edge)
            runs.append(("verify", v, st.model_complex, None))
            cost += v.cost_usd
            extractor.apply_verifier(doc_x, v.data)
            checks = run_checks(doc_x, st.amount_tolerance_abs, st.amount_tolerance_rel)
    except LLMRefusal as e:
        runs.append(("primary", None, model, str(e)))
        _persist_runs(document_id, runs)
        _finish_needs_human(document_id, doc_type, "Needs your review — automatic reading was declined for this document; please enter it manually")
        return
    except LLMError as e:
        runs.append(("primary", None, model, str(e)))
        _persist_runs(document_id, runs)
        raise

    if doc_x.legibility == "illegible":
        _persist_runs(document_id, runs)
        _finish_unreadable(document_id, doc_type, "The document can't be read reliably, even after enhancement. "
                                                  "Please rescan at 300 DPI or share the original.", cost, t0)
        return

    _persist_runs(document_id, runs)
    _persist_extraction(document_id, doc_x, checks, model, cost, t0, unreadable)


def _persist_runs(document_id: str, runs) -> None:
    with session_scope() as s:
        for pass_name, res, model, err in runs:
            _log_run(s, document_id, pass_name, res, model, err)
    runs.clear()


def _persist_extraction(document_id: str, doc_x: ExtractedDoc, checks: list[CheckResult], model: str, cost: float,
                        t0: float, unreadable_pages: list) -> None:
    st = get_settings()
    with session_scope() as s:
        doc = s.get(Document, document_id)
        tenant = s.get(Tenant, doc.tenant_id)
        vf = VENDOR_FIELD.get(doc_x.spec.doc_type)
        if vf and doc_x.v(vf):
            doc.vendor_key = pseudonymise(doc_x.v(vf))
        auto, hs, weights = _thresholds(s, doc_x.spec.doc_type, tenant)
        hist = _history(s, doc.tenant_id, doc.vendor_key, doc_x.spec.doc_type)
        scores = score_document(doc_x, checks, doc.quality_bucket or "poor_scan", hist, auto, hs,
                                st.doc_arithmetic_penalty, weights)
        for c in checks:
            s.add(ValidationResult(document_id=doc.id, check_id=c.check_id, status=c.status, severity=c.severity,
                                   message=c.message, field_keys=c.field_keys, details=c.details or None))
        flagged_labels = []
        for i, fv in enumerate(doc_x.all_fields()):
            sc = scores[fv.key]
            s.add(ExtractedField(
                document_id=doc.id, ordinal=i, key=fv.key, field_name=fv.spec.name,
                label=(f"Line {fv.line_index + 1} · {fv.spec.label}" if fv.line_index is not None else fv.spec.label),
                group=fv.spec.group, line_index=fv.line_index, value_type=fv.spec.type, is_high_stakes=fv.spec.high_stakes,
                value=fv.value, raw_text=fv.raw_text or None,
                alt_value=fv.alt_value if fv.agreement is False else None,
                page=fv.page, bbox=fv.bbox, score=sc.score, band=sc.band, needs_review=sc.needs_review,
                review_reason=sc.reason, reason_codes=sc.reason_codes, features=sc.features,
                status="pending" if sc.needs_review else "auto", final_value=None))
            if sc.needs_review:
                flagged_labels.append(f"{fv.spec.label} (line {fv.line_index + 1})" if fv.line_index is not None else fv.spec.label)
        if unreadable_pages:
            flagged_labels.append("unreadable page")
        doc.fields_total = len(scores)
        doc.fields_flagged = sum(1 for x in scores.values() if x.needs_review)
        doc.doc_score = round(min((x.score for x in scores.values()), default=0.0), 4)
        doc.arithmetic_ok = not any(c.failed and c.severity == "error" for c in checks)
        doc.model_used, doc.prompt_version = model, PROMPT_VERSION
        doc.languages = sorted(set(doc.languages or []) | set(doc_x.languages)) or None
        doc.has_handwriting = bool(doc.has_handwriting or doc_x.handwriting)
        doc.processed_at = utcnow()
        doc.processing_ms = int((time.monotonic() - t0) * 1000)
        doc.cost_usd = round(cost, 4)
        failed = [c for c in checks if c.failed]
        if doc.fields_flagged or unreadable_pages:
            doc.status = "needs_review"
            msg = review_headline(failed, flagged_labels)
            if unreadable_pages:
                msg += f" · page {unreadable_pages[0][0]} unreadable: {unreadable_pages[0][1]}"
            doc.status_message = msg
            _enter_review(s, doc, tenant)
        else:
            doc.status, doc.status_message = "ready", ready_message(doc.fields_total)
            if random.random() < st.qa_sample_rate:
                doc.is_qa_sample = True  # silent spot-check: measures true accuracy of auto-accepted docs
                _enter_review(s, doc, tenant)
            else:
                _shorten_retention(doc)
        if tenant.eval_retention_opt_in:
            try:
                raw = get_store().get(DOCS, doc.id, "source")
                get_store().put(EVAL, doc.id, "source", raw, utcnow() + timedelta(days=st.eval_retention_days))
            except BlobGone:
                pass
        meta = dict(status=doc.status, model=model, bucket=doc.quality_bucket, fields=doc.fields_total,
                    flagged=doc.fields_flagged, arithmetic_ok=doc.arithmetic_ok, ms=doc.processing_ms,
                    qa_sample=doc.is_qa_sample, prompt_version=PROMPT_VERSION)
        tid, did = doc.tenant_id, doc.id
    audit.record("document.extracted", tenant_id=tid, object_type="document", object_id=did, **meta)


def _shorten_retention(doc: Document) -> None:
    st = get_settings()
    doc.blob_expires_at = min(doc.blob_expires_at or utcnow(), utcnow() + timedelta(hours=st.doc_grace_after_done_hours))
    get_store().set_expiry(DOCS, doc.id, doc.blob_expires_at)


def _blank_fields(s, doc: Document, doc_type: str, reason: str) -> None:
    spec = SPECS[doc_type]
    for i, f in enumerate(spec.fields):
        s.add(ExtractedField(document_id=doc.id, ordinal=i, key=f.name, field_name=f.name, label=f.label, group=f.group,
                             line_index=None, value_type=f.type, is_high_stakes=f.high_stakes, value=None,
                             score=0.0, band="low", needs_review=True, review_reason=reason,
                             reason_codes=["manual_entry"], features={}, status="pending"))
    doc.fields_total = doc.fields_flagged = len(spec.fields)


def _finish_unreadable(document_id: str, doc_type: str, reason: str, cost: float, t0: float) -> None:
    with session_scope() as s:
        doc = s.get(Document, document_id)
        tenant = s.get(Tenant, doc.tenant_id)
        doc.status, doc.status_message = "unreadable", f"Couldn't read this document — {reason}"
        doc.doc_type = doc_type
        doc.processed_at, doc.processing_ms, doc.cost_usd = utcnow(), int((time.monotonic() - t0) * 1000), round(cost, 4)
        _blank_fields(s, doc, doc_type, "Document unreadable: enter manually from the original or request a rescan")
        _enter_review(s, doc, tenant)
        tid = doc.tenant_id
    audit.record("document.unreadable", tenant_id=tid, object_type="document", object_id=document_id)


def _finish_needs_human(document_id: str, doc_type: str | None, message: str) -> None:
    with session_scope() as s:
        doc = s.get(Document, document_id)
        tenant = s.get(Tenant, doc.tenant_id)
        doc.status, doc.status_message, doc.processed_at = "needs_review", message, utcnow()
        if doc_type:
            doc.doc_type = doc_type
            _blank_fields(s, doc, doc_type, "Enter manually from the document")
        _enter_review(s, doc, tenant)
        tid = doc.tenant_id
    audit.record("document.manual_required", tenant_id=tid, object_type="document", object_id=document_id)


def _finish_failed(document_id: str, message: str) -> None:
    with session_scope() as s:
        doc = s.get(Document, document_id)
        doc.status, doc.status_message, doc.processed_at = "failed", message, utcnow()
        tid = doc.tenant_id
    audit.record("document.failed", tenant_id=tid, object_type="document", object_id=document_id)


@jobs.handler("process_document")
def _job_process(payload: dict) -> None:
    process_document(payload["document_id"])


@jobs.handler("process_document:failed")
def _job_process_failed(payload: dict) -> None:
    with session_scope() as s:
        doc = s.get(Document, payload["document_id"])
        if doc and doc.status == "processing":
            doc.status = "failed"
            doc.status_message = "We couldn't process this document automatically. Our team has been notified; you can retry."
            tid = doc.tenant_id
        else:
            return
    audit.record("document.failed", tenant_id=tid, object_type="document", object_id=payload["document_id"])


@jobs.handler("retention_sweep")
def retention_sweep(_: dict | None = None) -> int:
    """Delete expired blobs; enforce the hard cap even if review is still pending."""
    st = get_settings()
    store = get_store()
    now = utcnow()
    n = 0
    with session_scope() as s:
        cap_before = now - timedelta(hours=st.doc_hard_cap_hours)
        docs = s.execute(select(Document).where(Document.blob_purged_at.is_(None),
                                                (Document.blob_expires_at <= now) | (Document.created_at <= cap_before))).scalars().all()
        for d in docs:
            store.delete(DOCS, d.id)
            d.blob_purged_at = now
            n += 1
        ids = [(d.tenant_id, d.id) for d in docs]
    for ns, did in store.purge_expired(now):
        pass
    for tid, did in ids:
        audit.record("document.blob_purged", tenant_id=tid, object_type="document", object_id=did)
    return n
