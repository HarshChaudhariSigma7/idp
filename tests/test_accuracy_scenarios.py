"""One test per accuracy scenario added in the 'accuracy engine' upgrade. Each asserts the
behaviour a finance reviewer would see: which fields are flagged, what is suggested, and why."""
import io
import random

import pypdfium2 as pdfium
from sqlalchemy import select

from sereno import jobs, pipeline
from sereno.db import session_scope
from sereno.eval.fake_model import TruthResponder
from sereno.eval.synth import invoice_truth, make_einvoice, make_invoice, render_invoice_pdf, to_jpeg_bytes
from sereno.extraction import repair
from sereno.extraction.doc_specs import INVOICE
from sereno.extraction.llm import FakeLLM, set_llm
from sereno.models import Document, ExtractedField, Template, Tenant, User, ValidationResult
from sereno.review import complete_document
from sereno.validation.checks import run_checks
from sereno.validation.context import CONFUSABLE
from tests.test_validation import build, failed


def process(sd, tenant_and_users, data=None, **kw):
    tid, users = tenant_and_users
    set_llm(FakeLLM(TruthResponder(sd, **kw)))
    data = data or (sd.pdf if sd.pdf else to_jpeg_bytes(sd.image))
    with session_scope() as s:
        did = pipeline.ingest_upload(s, tid, users["uploader"], "doc", data, "auto").document_id
    jobs.drain()
    return did


def fields(did):
    with session_scope() as s:
        return {f.key: f for f in s.execute(select(ExtractedField).where(ExtractedField.document_id == did)).scalars()}


def checks_of(did):
    with session_scope() as s:
        return {v.check_id: v for v in s.execute(select(ValidationResult).where(ValidationResult.document_id == did)).scalars()}


def doc(did):
    with session_scope() as s:
        return s.get(Document, did)


def misread_gstin(g: str) -> str:
    """Same GSTIN with one look-alike character swapped (as a scan would)."""
    for i in range(2, 14):
        if g[i] in CONFUSABLE:
            return g[:i] + CONFUSABLE[g[i]][0] + g[i + 1:]
    raise AssertionError("no confusable character")


# --- signed e-invoice QR ------------------------------------------------------------------------

def test_einvoice_qr_confirms_fields_and_document_goes_straight_through(tenant_and_users):
    sd = make_einvoice(random.Random(31), "good_scan", upi=True)
    did = process(sd, tenant_and_users)
    f = fields(did)
    for k in ("supplier_gstin", "buyer_gstin", "invoice_number", "invoice_date", "grand_total"):
        assert "matches e-invoice QR" in (f[k].evidence or []), k
        assert not f[k].needs_review
    assert doc(did).status == "ready"
    assert doc(did).evidence_summary["qr_confirmed"] >= 5
    assert checks_of(did)["line_count"].status == "pass"


def test_gstin_misread_by_both_passes_is_caught_by_the_qr(tenant_and_users):
    sd = make_einvoice(random.Random(32), "good_scan")
    bad = misread_gstin(sd.truth["supplier_gstin"])
    did = process(sd, tenant_and_users, errors={"primary": {"supplier_gstin": bad}, "secondary": {"supplier_gstin": bad}})
    g = fields(did)["supplier_gstin"]
    assert g.needs_review and g.suggested_value == sd.truth["supplier_gstin"]
    assert "e-invoice QR" in g.review_reason
    assert checks_of(did)["qr:supplier_gstin"].status == "fail"


def test_qr_says_more_line_items_than_were_read(tenant_and_users):
    sd = make_einvoice(random.Random(33), "good_scan", qr_overrides={"ItemCnt": 4})
    did = process(sd, tenant_and_users)
    c = checks_of(did)["line_count"]
    assert c.status == "fail" and "4 line items" in c.message
    assert doc(did).status == "needs_review"


def test_edited_print_contradicting_its_signed_qr(tenant_and_users):
    # the printed page adds up on its own, but the IRP-signed QR carries a different total
    sd = make_einvoice(random.Random(34), "good_scan", qr_overrides={"TotInvVal": 99999.0})
    did = process(sd, tenant_and_users)
    gt = fields(did)["grand_total"]
    assert gt.needs_review and "edited document" in gt.review_reason
    assert doc(did).arithmetic_ok is False  # QR mismatch is an error-severity finding


# --- hard documents: third blind read ------------------------------------------------------------

def test_handwritten_document_gets_three_readings_per_key_figure(tenant_and_users):
    sd = make_invoice(random.Random(35), "good_scan")
    did = process(sd, tenant_and_users, handwriting=True)
    f = fields(did)
    assert "3 of 3 readings agree" in (f["grand_total"].evidence or [])
    assert not f["grand_total"].needs_review
    assert doc(did).evidence_summary["votes_3of3"] >= 5


def test_hard_document_where_the_zoomed_crop_dissents(tenant_and_users):
    sd = make_invoice(random.Random(36), "good_scan")
    wrong = sd.truth["grand_total"] + 90
    did = process(sd, tenant_and_users, handwriting=True, errors={"crop": {"grand_total": wrong}})
    gt = fields(did)["grand_total"]
    # pages A and B agree with each other and with the arithmetic: the value is kept, the dissent is shown
    assert gt.value == sd.truth["grand_total"] and gt.needs_review


def test_missed_row_does_not_shift_every_later_row(tenant_and_users):
    sd = make_invoice(random.Random(37), "good_scan", n_lines=4)
    did = process(sd, tenant_and_users, drop_lines={"secondary": [1]})
    f = fields(did)
    for i in (0, 2, 3):  # rows after the missed one still align with their true partner
        assert not f[f"line_items[{i}].taxable_value"].needs_review, i
    assert checks_of(did)["line_count"].status == "fail"


# --- arithmetic-guided repair ---------------------------------------------------------------------

def test_look_alike_digit_suggestion_without_any_other_reading():
    t, l = invoice_truth(random.Random(4), 3, intra=True)
    s = f"{t['grand_total']:.2f}"
    swap = {"0": "8", "1": "7", "2": "7", "3": "8", "4": "9", "5": "6", "6": "5", "7": "1", "8": "3", "9": "4"}
    bad = float(s[:1] + swap[s[1]] + s[2:])
    d = build(INVOICE, dict(t, grand_total=bad), l)
    backed, sug = repair.search(d, run_checks(d), {}, 1.0, 0.00002)
    assert backed == [] and sug.key == "grand_total" and sug.value == t["grand_total"]


def test_whole_number_quantities_never_get_fractional_suggestions():
    assert all(float(v).is_integer() for v, _ in repair.slips(501.0, "501"))


# --- financial year, own GSTIN, vendor history, duplicates ------------------------------------------

def test_day_month_swap_caught_by_financial_year_in_invoice_number(tenant_and_users):
    sd = make_invoice(random.Random(38), "good_scan")
    sd.truth.update(invoice_number="INV/25-26/0007", invoice_date="2025-04-03")
    did = process(sd, tenant_and_users, errors={"primary": {"invoice_date": "2025-03-04"},
                                                 "secondary": {"invoice_date": "2025-03-04"}})
    d = fields(did)["invoice_date"]
    assert d.needs_review and d.suggested_value == "2025-04-03"
    assert "day and month were probably swapped" in d.review_reason


def test_buyer_gstin_repaired_from_company_master_data(tenant_and_users):
    tid, _ = tenant_and_users
    sd = make_invoice(random.Random(39), "good_scan")
    with session_scope() as s:
        s.get(Tenant, tid).settings = {"own_gstins": [sd.truth["buyer_gstin"]]}
    bad = misread_gstin(sd.truth["buyer_gstin"])
    did = process(sd, tenant_and_users, errors={"primary": {"buyer_gstin": bad}, "secondary": {"buyer_gstin": bad}})
    g = fields(did)["buyer_gstin"]
    assert g.needs_review and g.suggested_value == sd.truth["buyer_gstin"]
    assert "your GSTIN" in g.review_reason
    ok = process(make_invoice(random.Random(40), "good_scan"), tenant_and_users)
    assert checks_of(ok)["own_gstin"].status == "fail"  # billed to someone else's GSTIN: flagged as a warning


def _review(did, users):
    with session_scope() as s:
        d = s.get(Document, did)
        admin = s.get(User, users["admin"])
        for f in d.fields:
            if f.status == "pending":
                f.status = "confirmed"
        complete_document(s, admin, d, override_failed_checks=True)


def test_vendor_bank_account_change_is_an_error(tenant_and_users):
    tid, users = tenant_and_users
    rng = random.Random(41)
    first = make_invoice(rng, "good_scan")
    first.truth["bank_account_number"] = "50100123456789"
    _review(process(first, tenant_and_users), users)
    second = make_invoice(random.Random(42), "good_scan")
    second.truth.update(supplier_gstin=first.truth["supplier_gstin"], supplier_name=first.truth["supplier_name"],
                        bank_account_number="91900098765432")
    did = process(second, tenant_and_users)
    c = checks_of(did)["vendor_bank_changed"]
    assert c.status == "fail" and c.severity == "error" and "ending 5432" in c.message
    assert fields(did)["bank_account_number"].needs_review


def test_duplicate_invoice_is_flagged(tenant_and_users):
    sd = make_invoice(random.Random(43), "good_scan")
    process(sd, tenant_and_users)
    again = process(sd, tenant_and_users, data=to_jpeg_bytes(sd.image, q=80))  # re-scanned copy: different bytes
    c = checks_of(again)["duplicate"]
    assert c.status == "fail" and "same issuer and document number" in c.message
    assert fields(again)["invoice_number"].needs_review


def test_vendor_template_switches_on_after_three_reviewed_invoices(tenant_and_users):
    tid, users = tenant_and_users
    base = make_invoice(random.Random(44), "good_scan")
    for i in range(3):
        sd = make_invoice(random.Random(100 + i), "good_scan")
        sd.truth.update(supplier_gstin=base.truth["supplier_gstin"], supplier_name=base.truth["supplier_name"])
        _review(process(sd, tenant_and_users), users)
    with session_scope() as s:
        t = s.execute(select(Template).where(Template.tenant_id == tid)).scalars().all()
        assert len(t) == 1 and t[0].status == "active" and t[0].name.startswith("Auto:")


# --- vernacular -----------------------------------------------------------------------------------

def test_hindi_amount_in_words_is_checked():
    t, l = invoice_truth(random.Random(5), 2)
    ok = build(INVOICE, dict(t, grand_total=480.0, amount_in_words="चार सौ अस्सी रुपये मात्र"), [])
    bad = build(INVOICE, dict(t, grand_total=580.0, amount_in_words="चार सौ अस्सी रुपये मात्र"), [])
    assert "amount_in_words" not in failed(run_checks(ok))
    assert "amount_in_words" in failed(run_checks(bad))


def test_rate_per_hundred_units_is_not_a_false_alarm():
    t = {"subtotal": 450.0}
    lines = [{"quantity": 100.0, "rate": 450.0, "rate_per": "100 NOS", "taxable_value": 450.0}]
    assert "line_math:0" not in failed(run_checks(build(INVOICE, t, lines)))


# --- batch scans ------------------------------------------------------------------------------------

class _BatchResponder:
    """Answers for whichever invoice the request is about (digital PDFs carry their text layer)."""

    def __init__(self, sds):
        self.by_number = {sd.truth["invoice_number"]: TruthResponder(sd) for sd in sds}
        self.first = next(iter(self.by_number.values()))

    def __call__(self, pass_name, schema, content):
        n_images = sum(1 for b in content if b.get("type") == "image")
        if pass_name == "triage" and n_images > 1:
            return TruthResponder(self.first.sd, doc_index={i: i for i in range(1, n_images + 1)})(pass_name, schema, content)
        text = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        for num, r in self.by_number.items():
            if num in text:  # primary pass carries the PDF text layer: remember which invoice this is
                self.current = r
                return r(pass_name, schema, content)
        # later passes (image-only) belong to the document whose primary pass ran last
        return getattr(self, "current", self.first)(pass_name, schema, content)


def test_batch_pdf_is_split_into_separate_documents(tenant_and_users):
    tid, users = tenant_and_users
    rng = random.Random(45)
    sds = []
    for _ in range(2):
        t, l = invoice_truth(rng, 2)
        from sereno.eval.synth import SynthDoc
        sds.append(SynthDoc("invoice", t, l, pdf=render_invoice_pdf(t, l)))
    merged = pdfium.PdfDocument.new()
    for sd in sds:
        merged.import_pages(pdfium.PdfDocument(sd.pdf))
    buf = io.BytesIO()
    merged.save(buf)
    set_llm(FakeLLM(_BatchResponder(sds)))
    with session_scope() as s:
        parent_id = pipeline.ingest_upload(s, tid, users["uploader"], "batch.pdf", buf.getvalue(), "invoice").document_id
    jobs.drain()
    with session_scope() as s:
        parent = s.get(Document, parent_id)
        kids = s.execute(select(Document).where(Document.parent_id == parent_id).order_by(Document.created_at)).scalars().all()
        assert parent.status == "split" and len(kids) == 2
        numbers = sorted(next(f.value for f in k.fields if f.key == "invoice_number") for k in kids)
        assert numbers == sorted(sd.truth["invoice_number"] for sd in sds)
        assert all(k.status == "ready" for k in kids)
