import random

from sqlalchemy import select

from sereno import jobs, pipeline
from sereno.db import session_scope
from sereno.eval.fake_model import TruthResponder
from sereno.eval.synth import make_invoice, make_lr, to_jpeg_bytes
from sereno.extraction.llm import FakeLLM, set_llm
from sereno.models import AuditEvent, Correction, Document, ExtractedField, ExtractionRun, User, VendorFieldStat
from sereno.review import act_on_field, complete_document
from sereno.security.audit import verify_chain
from sereno.storage.temp_store import DOCS, get_store


def run(sd, tenant_and_users, **resp_kwargs):
    tid, users = tenant_and_users
    llm = FakeLLM(TruthResponder(sd, **resp_kwargs))
    set_llm(llm)
    data = sd.pdf if sd.pdf else to_jpeg_bytes(sd.image)
    with session_scope() as s:
        out = pipeline.ingest_upload(s, tid, users["uploader"], "doc.jpg", data, "auto")
    jobs.drain()
    return out.document_id, llm


def fields_of(doc_id):
    with session_scope() as s:
        return {f.key: f for f in s.execute(select(ExtractedField).where(ExtractedField.document_id == doc_id)).scalars()}


def doc_of(doc_id):
    with session_scope() as s:
        return s.get(Document, doc_id)


def test_clean_digital_invoice_is_ready_with_cheap_model(tenant_and_users):
    sd = make_invoice(random.Random(10), "digital")
    doc_id, llm = run(sd, tenant_and_users)
    d = doc_of(doc_id)
    assert d.status == "ready", d.status_message
    assert d.quality_bucket == "digital" and d.is_digital
    assert d.model_used == "gemini-3.1-flash-lite"
    assert d.status_message.startswith("Ready to export")
    f = fields_of(doc_id)
    assert f["grand_total"].value == sd.truth["grand_total"]
    assert f["grand_total"].band == "high"
    assert all(not x.needs_review for x in f.values())
    passes = [c["pass"] for c in llm.calls]
    assert passes == ["triage", "primary", "secondary"]  # type unknown -> triage; clean -> no verifier
    with session_scope() as s:
        assert s.execute(select(ExtractionRun).where(ExtractionRun.document_id == doc_id)).scalars().all()


def test_scan_uses_strong_model_and_routes_only_the_disputed_field(tenant_and_users):
    sd = make_invoice(random.Random(11), "good_scan")
    wrong = sd.lines[1]["quantity"] + 10
    doc_id, llm = run(sd, tenant_and_users, errors={"primary": {"line_items[1].quantity": wrong}})
    d = doc_of(doc_id)
    assert d.model_used == "gemini-3.1-flash-lite"
    # the disagreement is settled by a blind third read of a zoomed crop, not by the verifier
    assert [c["pass"] for c in llm.calls] == ["triage", "primary", "secondary", "crop"]
    f = fields_of(doc_id)
    q = f["line_items[1].quantity"]
    # 2 of 3 readings restore the right value, but the field still goes to a human
    assert q.value == sd.lines[1]["quantity"] and q.needs_review and q.band == "low"
    assert d.status == "needs_review"
    assert "2 of 3 independent readings" in q.review_reason
    assert "2 of 3 readings agree" in (q.evidence or [])
    # fields that both passes agreed on and that reconcile stay auto-accepted
    assert not f["invoice_number"].needs_review
    assert [k for k, x in f.items() if x.needs_review] == ["line_items[1].quantity"], _debug_flags(doc_id)
    assert d.review_due_at is not None and d.assigned_to is not None


def test_correlated_misread_is_repaired_from_zoomed_reread(tenant_and_users):
    sd = make_invoice(random.Random(12), "good_scan")
    bad_total = sd.truth["grand_total"] + 1000
    # both passes misread identically => self-consistency can't catch it; the arithmetic fails,
    # the zoomed re-read sees the true total, and the repair makes every equation balance
    doc_id, llm = run(sd, tenant_and_users, errors={"primary": {"grand_total": bad_total},
                                                     "secondary": {"grand_total": bad_total}})
    d = doc_of(doc_id)
    f = fields_of(doc_id)
    assert "crop" in [c["pass"] for c in llm.calls]
    gt = f["grand_total"]
    assert gt.value == sd.truth["grand_total"] and gt.alt_value == bad_total
    assert gt.needs_review and "Corrected from" in gt.review_reason  # a person still confirms
    assert d.status == "needs_review" and d.arithmetic_ok
    assert [k for k, x in f.items() if x.needs_review] == ["grand_total"]  # not 11 fields any more


def test_document_that_really_does_not_add_up(tenant_and_users):
    sd = make_invoice(random.Random(13), "good_scan")
    bad_total = sd.truth["grand_total"] + 1000
    # the zoomed re-read ALSO sees the bad total: the paper itself is wrong -> say so, don't "fix" it
    doc_id, _ = run(sd, tenant_and_users, errors={"primary": {"grand_total": bad_total},
                                                   "secondary": {"grand_total": bad_total},
                                                   "crop": {"grand_total": bad_total}})
    d = doc_of(doc_id)
    f = fields_of(doc_id)
    assert d.status == "needs_review" and not d.arithmetic_ok
    assert f["grand_total"].value == bad_total and f["grand_total"].needs_review
    assert "total" in d.status_message
    with session_scope() as s:
        from sereno.models import ValidationResult
        notes = [v.message for v in s.query(ValidationResult).filter(ValidationResult.document_id == doc_id,
                                                                    ValidationResult.status == "note")]
    assert any("document itself doesn't add up" in n for n in notes)
    clean = make_invoice(random.Random(99), "good_scan")
    doc2, _ = run(clean, tenant_and_users)
    f2 = fields_of(doc2)
    # whole-document penalty: the same field scores lower on the document that doesn't add up
    assert f2["invoice_number"].score > f["invoice_number"].score


def test_unreadable_document_is_not_extracted(tenant_and_users):
    sd = make_invoice(random.Random(13), "unreadable")
    doc_id, llm = run(sd, tenant_and_users)
    d = doc_of(doc_id)
    assert d.status == "unreadable"
    assert "Couldn't read this document" in d.status_message
    assert "primary" not in [c["pass"] for c in llm.calls]
    assert all(f.needs_review for f in fields_of(doc_id).values())


def test_bilingual_lr_routes_to_opus_and_extracts(tenant_and_users):
    sd = make_lr(random.Random(14), "carbon")
    doc_id, llm = run(sd, tenant_and_users)
    d = doc_of(doc_id)
    assert d.doc_type == "lr" and d.model_used == "gemini-3.1-flash-lite"
    assert "hindi" in (d.languages or [])
    f = fields_of(doc_id)
    assert f["total_freight"].value == sd.truth["total_freight"]


def test_review_flow_logs_corrections_and_updates_vendor_history(tenant_and_users):
    tid, users = tenant_and_users
    sd = make_invoice(random.Random(15), "good_scan")
    # all three readings agree on the wrong rate, so only a person can fix it
    doc_id, _ = run(sd, tenant_and_users, errors={"primary": {"line_items[0].rate": 1.0}, "secondary": {"line_items[0].rate": 1.0},
                                                   "crop": {"line_items[0].rate": 1.0}})
    with session_scope() as s:
        d = s.get(Document, doc_id)
        reviewer = s.get(User, d.assigned_to)
        flagged = [f for f in d.fields if f.needs_review]
        assert flagged
        for f in flagged:
            if f.key == "line_items[0].rate":
                act_on_field(s, reviewer, d, f, "correct", str(sd.lines[0]["rate"]), 4200)
            else:
                act_on_field(s, reviewer, d, f, "confirm", None, 900)
        res = complete_document(s, reviewer, d)
        assert res["completed"], res
        assert d.status == "ready" and d.reviewed_at
    with session_scope() as s:
        corr = s.execute(select(Correction).where(Correction.document_id == doc_id)).scalars().all()
        assert any(c.action == "correct" and c.new_value == sd.lines[0]["rate"] for c in corr)
        stats = {v.field_name: v for v in s.execute(select(VendorFieldStat)).scalars()}
        assert stats["line.rate"].total == 1 and stats["line.rate"].correct == 0
    ok, bad = verify_chain()
    assert ok


def test_retention_sweep_deletes_blobs(tenant_and_users):
    from datetime import timedelta
    sd = make_invoice(random.Random(16), "digital")
    doc_id, _ = run(sd, tenant_and_users)
    with session_scope() as s:
        d = s.get(Document, doc_id)
        d.blob_expires_at = d.blob_expires_at - timedelta(days=30)
    assert pipeline.retention_sweep({}) == 1
    assert not get_store().exists(DOCS, doc_id, "source")
    with session_scope() as s:
        assert s.get(Document, doc_id).blob_purged_at is not None
        events = [e.event for e in s.execute(select(AuditEvent)).scalars()]
    assert "document.blob_purged" in events


def test_audit_log_never_contains_pii(tenant_and_users):
    sd = make_invoice(random.Random(17), "good_scan")
    run(sd, tenant_and_users)
    with session_scope() as s:
        blob = " ".join(str(e.meta) + str(e.event) for e in s.execute(select(AuditEvent)).scalars())
    for secret in (sd.truth["supplier_gstin"], sd.truth["supplier_name"], sd.truth["invoice_number"], str(sd.truth["grand_total"])):
        assert secret not in blob


def _debug_flags(doc_id):
    return {k: (x.score, x.reason_codes, x.value) for k, x in fields_of(doc_id).items() if x.needs_review}


def test_retry_after_transient_api_error_is_idempotent(tenant_and_users):
    from sereno.extraction.llm import LLMError
    from sereno.models import Job, Page
    tid, users = tenant_and_users
    sd = make_invoice(random.Random(18), "good_scan")
    good = TruthResponder(sd)
    state = {"n": 0}

    def flaky(pass_name, schema, content):
        if pass_name == "primary" and state["n"] == 0:
            state["n"] += 1
            raise LLMError("API error 529")
        return good(pass_name, schema, content)
    set_llm(FakeLLM(flaky))
    with session_scope() as s:
        doc_id = pipeline.ingest_upload(s, tid, users["uploader"], "x.jpg", to_jpeg_bytes(sd.image), "auto").document_id
    jobs.drain()
    with session_scope() as s:  # make the scheduled retry runnable now
        for j in s.query(Job).filter(Job.status == "queued"):
            j.run_after = j.created_at
    jobs.drain()
    d = doc_of(doc_id)
    assert d.status == "ready", d.status_message
    with session_scope() as s:
        assert s.query(Page).filter(Page.document_id == doc_id).count() == 1
        n_fields = s.query(ExtractedField).filter(ExtractedField.document_id == doc_id).count()
        assert n_fields == d.fields_total
        assert s.query(ExtractionRun).filter(ExtractionRun.document_id == doc_id, ExtractionRun.error.is_not(None)).count() == 1
