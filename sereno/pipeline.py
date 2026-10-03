"""End-to-end document processing: ingest -> (batch split) -> type from free signals -> pre-check ->
correct -> page reading -> deterministic verification -> zoomed re-read of unproven values ->
arithmetic repair -> score -> route fields to people -> persist -> audit."""
from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

import threading

from sqlalchemy import func, select, text

from sereno import jobs
from sereno import masterdata
from sereno.config import get_settings
from sereno.confidence.scorer import score_document
from sereno.db import session_scope
from sereno.extraction import codes, crossread, doctype, evidence, extractor, repair
from sereno.extraction.doc_specs import SPECS, TRIAGE_SCHEMA, spec_with_extras
from sereno.extraction.llm import LLMClient, LLMError, LLMRefusal, LLMResult, get_llm, image_block, text_block
from sereno.extraction.prompts import PROMPT_VERSION, TRIAGE_SYSTEM
from sereno.extraction.normalize import normalise, values_agree
from sereno.extraction.result import ExtractedDoc
from sereno.ingest import loader, preprocess, quality
from sereno.models import (Document, ExtractedField, ExtractionRun, Page, Template, Tenant, ThresholdConfig, User,
                           ValidationResult, VendorFieldStat, utcnow)
from sereno.routing import ready_message, review_headline
from sereno.security import audit
from sereno.security.crypto import pseudonymise
from sereno.storage.temp_store import DOCS, EVAL, BlobGone, get_store
from sereno.validation.checks import CheckResult, run_checks
from sereno.validation.context import gstin_repairs

log = logging.getLogger("sereno.pipeline")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_persist_lock = threading.Lock()
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
    # Identical bytes seen before (same file re-uploaded, e.g. after a failed first attempt, or
    # testing with the same sample) are informational, never blocking: the file is always
    # ingested and processed as its own new document. `duplicate=True` just tells the UI this
    # exact file was seen before, so it can show a note -- the same policy as every other
    # duplicate-shaped signal in the product (GSTIN+invoice-number matches at review time): flag
    # for a person, never silently refuse or silently reuse the old result.
    seen_before = bool(s.execute(select(func.count()).select_from(Document).where(
        Document.tenant_id == tenant_id, Document.content_sha256 == sha,
        Document.blob_purged_at.is_(None))).scalar())
    if seen_before:
        audit.record("document.upload_duplicate", session=s, tenant_id=tenant_id, actor_id=user_id,
                     object_type="document", object_id=None, content_sha256=sha[:12])
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
    return UploadOutcome(doc.id, seen_before)


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
    edge = 1000 if len(pages) <= 6 else 700  # type, rotation and page boundaries need little resolution
    for p in pages[:40]:
        content.append(text_block(f"Page {p.page_no}:"))
        content.append(image_block(loader.encode_jpeg(preprocess.fit_long_edge(p.image, edge), 85), "image/jpeg"))
    content.append(text_block("Triage this document."))
    return llm.structured(pass_name="triage", model=model, system=TRIAGE_SYSTEM, content=content,
                          schema=TRIAGE_SCHEMA, max_tokens=4000, effort="low")


GUESS_MIN_DOCS, GUESS_MIN_SHARE = 20, 0.85


def _guess_type(tenant_id: str) -> str | None:
    """The company's dominant document type, when its recent history makes the guess safe. A wrong
    guess costs a repeated page reading; below ~85% the cheap triage call is the better bet."""
    with session_scope() as s:
        recent = s.execute(select(Document.doc_type).where(Document.tenant_id == tenant_id, Document.doc_type.is_not(None))
                           .order_by(Document.created_at.desc()).limit(200)).scalars().all()
    if len(recent) < GUESS_MIN_DOCS:
        return None
    top, n = Counter(recent).most_common(1)[0]
    return top if top in SPECS and n / len(recent) >= GUESS_MIN_SHARE else None


def _extra_fields(s, tenant_id: str, doc_type: str) -> list[dict]:
    """Custom fields from every active template of this type, so they are read even before the
    vendor is known (scans without a QR code)."""
    out, seen = [], set()
    for t in s.execute(select(Template).where(Template.tenant_id == tenant_id, Template.doc_type == doc_type,
                                              Template.status == "active")).scalars():
        for e in t.extra_fields or []:
            if e.get("name") not in seen:
                seen.add(e.get("name"))
                out.append(e)
    return out[:20]


def process_document(document_id: str, llm: LLMClient | None = None, type_hint: str | None = None) -> None:
    """Call budget (docs/ARCHITECTURE.md): one page reading; one zoomed re-read only for values
    nothing else could prove; a cheap triage call only when free signals can't name the type or a
    multi-page scan needs splitting; a repeat reading only when a guessed type was wrong. Typical:
    1 call for a digital PDF or e-invoice, 1-2 for a scan of a known type."""
    st = get_settings()
    llm = llm or get_llm()
    store = get_store()
    t0 = time.monotonic()
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None or doc.status not in ("uploaded", "processing", "failed"):
            return
        doc.status, doc.status_message = "processing", "Reading…"
        tenant_id, declared, parent_id = doc.tenant_id, doc.declared_type, doc.parent_id
        # idempotent on retry: derived rows are rebuilt; extraction_runs are kept (they are the log)
        for model in (Page, ExtractedField, ValidationResult):
            s.query(model).filter(model.document_id == document_id).delete(synchronize_session=False)
        own = masterdata.own_gstins(s.get(Tenant, tenant_id))
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
    text_all = "\n".join(p.text or "" for p in loaded.pages if p.native_text)

    # --- 1. one document or a batch, and what type? Free signals first; a cheap low-resolution
    # triage call only when they can't tell (batch scans, unknown scans) -------------------------
    code_readings = codes.read_codes([(p.page_no, p.image) for p in loaded.pages[:6]], st.einvoice_public_keys_pem)
    free_type = (declared if declared in SPECS else
                 "invoice" if any(r.source == "einvoice_qr" for r in code_readings) else
                 doctype.from_text(text_all) or (type_hint if type_hint in SPECS else None))
    multi = len(loaded.pages) > 1 and parent_id is None
    local = doctype.local_groups([p.text for p in loaded.pages]) if (multi and loaded.is_digital) else None
    guessed = free_type is None and not multi and _guess_type(tenant_id)
    need_triage = (multi and local is None and not (loaded.is_digital and len(loaded.pages) <= 4)) or \
                  (free_type is None and not guessed)
    triage = None
    groups = local or [list(range(1, len(loaded.pages) + 1))]
    if need_triage:
        try:
            tr = _triage(llm, loaded.pages, st.model_triage)
            triage, cost = tr.data, cost + tr.cost_usd
            runs.append(("triage", tr, st.model_triage, None))
            if multi:
                groups = _document_groups(triage, len(loaded.pages))
        except LLMError as e:
            log.warning("triage failed for document %s: %s", document_id, e)
            runs.append(("triage", None, st.model_triage, str(e)))
    if len(groups) > 1 and parent_id is None:
        _persist_runs(document_id, runs)
        _split_batch(document_id, raw, loaded.mime, groups, (triage or {}).get("document_type"))
        return

    doc_type = free_type or (triage or {}).get("document_type") or guessed or "invoice"
    if doc_type not in SPECS:
        _persist_runs(document_id, runs)
        _finish_needs_human(document_id, doc_type=None, message="Not an invoice, LR, PO, GRN or contract")
        return
    pages_in = loaded.pages[:SPECS[doc_type].max_pages]
    if triage:
        rotations = {p.get("page"): p.get("rotation_needed", 0) for p in triage.get("pages", [])}
    else:
        rotations = {p.page_no: quality.quarter_turn_hint(p.image) for p in pages_in if not p.native_text}

    # --- 3. pre-check + auto-correct (local) -----------------------------------------------------
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

    issuer = ((triage or {}).get("issuer_gstin")
              or next((r.fields.get("supplier_gstin") for r in code_readings if r.source == "einvoice_qr"), None)
              or doctype.issuer_gstin(text_all, own))
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
        if issuer:
            doc.vendor_key = pseudonymise(issuer)
        template = None
        if doc.vendor_key:
            template = s.execute(select(Template).where(Template.tenant_id == tenant_id, Template.vendor_key == doc.vendor_key,
                                                        Template.doc_type == doc_type, Template.status == "active")
                                 .order_by(Template.created_at.desc())).scalars().first()
        template_hints = template.hints if template else None
        doc.template_id = template.id if template else None
        extra_fields = _extra_fields(s, tenant_id, doc_type)
        bucket = doc.quality_bucket

    audit.record("document.precheck", tenant_id=tenant_id, object_type="document", object_id=document_id,
                 bucket=bucket, pages=len(page_rows), unreadable_pages=len(unreadable))

    if unreadable and len(unreadable) == len(prepared):
        _persist_runs(document_id, runs)
        _finish_unreadable(document_id, doc_type, unreadable[0][1], cost, t0)
        return

    # --- 4. the page reading (1 call) ------------------------------------------------------------
    langs = set((triage or {}).get("languages") or [])
    indic_text = bool(_DEVANAGARI.search(text_all))
    hard_pre = (bucket == "poor_scan" or doc_type == "lr" or indic_text or bool((triage or {}).get("handwriting_present"))
                or any(l not in ("english", "other") for l in langs))
    model = st.model_complex if hard_pre else st.model_clean
    effort = st.extraction_effort_hard if hard_pre else st.extraction_effort
    edge = {"digital": st.page_edge_digital, "good_scan": st.page_edge_good}.get(bucket, st.max_image_long_edge)

    def read_page(dt: str) -> ExtractedDoc:
        nonlocal cost
        spec = spec_with_extras(dt, extra_fields)
        res, frames = extractor.run_primary(llm, spec, prepared, model, edge, template_hints, effort=effort)
        runs.append(("primary", res, model, None))
        cost += res.cost_usd
        d = extractor.parse_primary(spec, res.data)
        extractor.remap_locations(d, frames)
        return d

    try:
        doc_x = read_page(doc_type)
        observed = doc_x.observed_type
        if observed and observed != doc_type and declared not in SPECS:
            if observed not in SPECS:
                _persist_runs(document_id, runs)
                _finish_needs_human(document_id, doc_type=None, message="Not an invoice, LR, PO, GRN or contract")
                return
            doc_type = observed  # the free signals guessed wrong: read again with the right fields
            doc_x = read_page(doc_type)
            with session_scope() as s:
                s.get(Document, document_id).doc_type = doc_type
        elif observed and observed != doc_type:
            doc_x.anomalies.append(f"Uploaded as {SPECS[doc_type].label} but looks like "
                                   f"{SPECS[observed].label if observed in SPECS else 'something else'}")

        # --- 5. deterministic verification (free) ------------------------------------------------
        extractor.ground_in_text_layer(doc_x, prepared)
        doc_x.anomalies += codes.apply_codes(doc_x, code_readings)
        with session_scope() as s:
            ctx, _ = masterdata.build_context(s, s.get(Tenant, tenant_id), doc_x, document_id)
        checks = run_checks(doc_x, st.amount_tolerance_abs, st.amount_tolerance_rel, ctx=ctx)
        evidence.mark_verified(doc_x, checks)

        # --- 6. one zoomed re-read of what nothing proved (0 or 1 call) --------------------------
        hard = hard_pre or doc_x.handwriting or any(l not in ("english", "other") for l in doc_x.languages)
        targets = crossread.select_targets(doc_x, hard, st.max_verify_items) if st.crop_reads else []
        if targets:
            res, reads = crossread.run_crop_reads(llm, targets, prepared, st.model_verify, st.verify_effort)
            if res is not None:
                runs.append(("crop", res, st.model_verify, None))
                cost += res.cost_usd
            crossread.apply_reads(doc_x, reads)
            checks = run_checks(doc_x, st.amount_tolerance_abs, st.amount_tolerance_rel, ctx=ctx)

        # --- 7. figures still don't reconcile: let the arithmetic pick between the readings ------
        if st.arithmetic_repair and repair.failing_arithmetic(checks):
            implicated = repair.implicated_numeric(doc_x, checks)
            proof = {f.key: [(f.alt_value, "zoomed reading"), (f.code_value, "QR code")] for f in implicated}
            backed, suggestion = repair.search(doc_x, checks, proof, st.amount_tolerance_abs, st.amount_tolerance_rel)
            repair.apply(doc_x, backed, suggestion)
            if not backed and implicated and all(f.double_read and f.agreement for f in implicated):
                doc_x.anomalies.append("A zoomed re-read confirms the printed figures: the document itself "
                                       "doesn't add up. Query the vendor before booking it.")
    except LLMRefusal as e:
        runs.append(("primary", None, model, str(e)))
        _persist_runs(document_id, runs)
        _finish_needs_human(document_id, doc_type, "Automatic reading was declined for this document; enter it manually")
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
    _persist_extraction(document_id, doc_x, model, cost, t0, unreadable)


def _document_groups(triage: dict | None, n_pages: int) -> list[list[int]]:
    """Page numbers per document from triage's document_index (contiguous runs, in page order)."""
    if not triage or n_pages < 2:
        return [list(range(1, n_pages + 1))]
    idx = {p.get("page"): p.get("document_index") or 1 for p in triage.get("pages", [])}
    groups: dict[int, list[int]] = {}
    for page in range(1, n_pages + 1):
        groups.setdefault(int(idx.get(page, 1)), []).append(page)
    return [sorted(v) for _, v in sorted(groups.items())]


def _subset(raw: bytes, mime: str, pages: list[int]) -> bytes:
    import io

    from PIL import Image
    if mime == "application/pdf":
        import pypdfium2 as pdfium
        src, dst = pdfium.PdfDocument(raw), pdfium.PdfDocument.new()
        dst.import_pages(src, [p - 1 for p in pages])
        buf = io.BytesIO()
        dst.save(buf)
        dst.close()
        src.close()
        return buf.getvalue()
    img = Image.open(io.BytesIO(raw))
    frames = []
    for p in pages:
        img.seek(p - 1)
        frames.append(img.convert("RGB").copy())
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:], compression="tiff_deflate")
    return buf.getvalue()


def _split_batch(document_id: str, raw: bytes, mime: str, groups: list[list[int]], type_hint: str | None = None) -> None:
    """A batch scan holding several documents becomes one document per group of pages, each
    processed, reviewed and exported on its own."""
    child_ids = []
    with session_scope() as s:
        parent = s.get(Document, document_id)
        name = str(parent.filename)
        for k, pages in enumerate(groups, 1):
            data = _subset(raw, mime, pages)
            child = Document(tenant_id=parent.tenant_id, uploaded_by=parent.uploaded_by,
                             filename=f"{name} (document {k} of {len(groups)}, pages {pages[0]}-{pages[-1]})"[:255],
                             content_sha256=hashlib.sha256(data).hexdigest(),
                             mime_type="application/pdf" if mime == "application/pdf" else "image/tiff",
                             declared_type="auto", status="uploaded", parent_id=parent.id,
                             blob_expires_at=parent.blob_expires_at)
            s.add(child)
            s.flush()
            get_store().put(DOCS, child.id, "source", data, child.blob_expires_at)
            jobs.enqueue(s, "process_document", {"document_id": child.id, "type_hint": type_hint})
            child_ids.append(child.id)
        parent.status = "split"
        parent.status_message = f"Batch scan split into {len(groups)} documents, each read separately"
        parent.page_count = sum(len(g) for g in groups)
        _shorten_retention(parent)
        tid = parent.tenant_id
    audit.record("document.split", tenant_id=tid, object_type="document", object_id=document_id,
                 parts=len(groups), children=child_ids)


def _persist_runs(document_id: str, runs) -> None:
    with session_scope() as s:
        for pass_name, res, model, err in runs:
            _log_run(s, document_id, pass_name, res, model, err)
    runs.clear()


def _suggest_from_checks(doc_x: ExtractedDoc, checks: list[CheckResult]) -> None:
    """Turn check findings into ready-to-accept suggestions (still confirmed by a person)."""
    by_key = doc_x.by_key()
    for c in checks:
        if not c.failed or not c.field_keys:
            continue
        fv = by_key.get(c.field_keys[0])
        if fv is None or fv.suggested_value is not None or fv.repaired:
            continue
        if (c.details or {}).get("suggest"):
            fv.suggested_value = normalise(fv.spec.type, c.details["suggest"])
            fv.suggestion_reason = c.message
        elif c.check_id.startswith("qr:"):
            fv.suggested_value, fv.suggestion_reason = fv.code_value, "value encoded in the signed e-invoice QR"
        elif c.check_id.startswith("format:") and fv.spec.type == "gstin":
            options = gstin_repairs(fv.value)
            if len(options) == 1:
                fv.suggested_value = options[0]
                fv.suggestion_reason = "the only valid GSTIN one look-alike character away from what was read"


def _persist_extraction(document_id: str, doc_x: ExtractedDoc, model: str, cost: float,
                        t0: float, unreadable_pages: list) -> None:
    st = get_settings()
    # Duplicate detection must see documents committed by parallel workers: the check and the save
    # happen under one lock (per process; plus a Postgres advisory lock across processes).
    with _persist_lock, session_scope() as s:
        if s.bind.dialect.name == "postgresql":
            s.execute(text("SELECT pg_advisory_xact_lock(424243)"))
        doc = s.get(Document, document_id)
        tenant = s.get(Tenant, doc.tenant_id)
        vf = VENDOR_FIELD.get(doc_x.spec.doc_type)
        if vf and doc_x.v(vf):
            doc.vendor_key = pseudonymise(doc_x.v(vf))
        ctx, doc.dedupe_key = masterdata.build_context(s, tenant, doc_x, doc.id)
        checks = run_checks(doc_x, st.amount_tolerance_abs, st.amount_tolerance_rel, ctx=ctx)
        evidence.mark_verified(doc_x, checks)  # final state: a repair or a re-read may change what is proven
        _suggest_from_checks(doc_x, checks)
        for note in dict.fromkeys(doc_x.anomalies):
            checks.append(CheckResult("note", "note", str(note)[:500], [], "warn"))
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
                alt_value=fv.alt_value if (fv.agreement is False or fv.code_agrees is False or fv.repaired) else None,
                page=fv.page, bbox=fv.bbox, score=sc.score, band=sc.band, needs_review=sc.needs_review,
                review_reason=sc.reason, reason_codes=sc.reason_codes, features=sc.features,
                status="pending" if sc.needs_review else "auto", final_value=None,
                suggested_value=fv.suggested_value, suggestion_reason=fv.suggestion_reason,
                evidence=list(dict.fromkeys(fv.evidence + evidence.tags(fv))) or None))
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
        allf = doc_x.all_fields()
        doc.evidence_summary = {
            "codes": sorted({r.source for r in doc_x.codes}),
            "qr_confirmed": sum(1 for f in allf if f.code_agrees and not f.code_filled),
            "qr_filled": sum(1 for f in allf if f.code_filled),
            "qr_mismatch": sum(1 for f in allf if f.code_agrees is False),
            "proven": sum(1 for f in allf if f.verified_by),
            "zoom_confirmed": sum(1 for f in allf if f.double_read and f.agreement),
            "zoom_differs": sum(1 for f in allf if f.double_read and f.agreement is False),
            "repaired": sum(1 for f in allf if f.repaired),
            "suggestions": sum(1 for f in allf if f.suggested_value is not None),
            "master_matches": sum(1 for f in allf if f.master_match),
            "duplicates": len(ctx.duplicates),
        }
        failed = [c for c in checks if c.failed]
        if doc.fields_flagged or unreadable_pages:
            doc.status = "needs_review"
            msg = review_headline(failed, flagged_labels)
            if unreadable_pages:
                msg += f" · page {unreadable_pages[0][0]} unreadable: {unreadable_pages[0][1]}"
            doc.status_message = msg
            # spot checks sample every document, flagged or not: the reviewer confirms every field,
            # which gives unbiased labels at any threshold (eval/backtest.py calibrates on these)
            doc.is_qa_sample = random.random() < st.qa_sample_rate
            _enter_review(s, doc, tenant)
        else:
            doc.status, doc.status_message = "ready", ready_message(doc.fields_total)
            if random.random() < st.qa_sample_rate:
                doc.is_qa_sample = True  # silent spot-check: measures true accuracy of auto-accepted docs
                _enter_review(s, doc, tenant)
            else:
                _shorten_retention(doc)
            s.flush()
            s.expire(doc, ["fields"])
            masterdata.learn(s, doc)
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
        doc.status, doc.status_message = "unreadable", f"Couldn't read this document: {reason}"
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
    process_document(payload["document_id"], type_hint=payload.get("type_hint"))


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
