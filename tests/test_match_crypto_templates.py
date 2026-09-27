import random

import pytest

from sereno.extraction.doc_specs import GRN, INVOICE, PO
from sereno.matching.three_way import three_way_match
from sereno.security import crypto
from sereno.security.audit import AuditPIIError, record, verify_chain
from tests.test_validation import build


def _po():
    return build(PO, {"po_number": "PO/4411", "supplier_gstin": "27AAPFU0939F1ZV", "subtotal": 30000.0},
                 [{"item_code": "BLT12", "description": "MS Hex Bolt M12x50", "quantity": 1000.0, "rate": 12.0,
                   "gst_rate": 18.0, "line_total": 12000.0},
                  {"item_code": "BRG6205", "description": "Ball Bearing 6205 ZZ", "quantity": 100.0, "rate": 180.0,
                   "gst_rate": 18.0, "line_total": 18000.0}])


def _grn(acc_bolts=1000.0):
    return build(GRN, {"grn_number": "GRN-77", "po_reference": "4411", "supplier_gstin": "27AAPFU0939F1ZV",
                       "supplier_invoice_reference": "INV/0091"},
                 [{"description": "M.S. HEX BOLT M12 X 50", "quantity_received": 1000.0, "quantity_accepted": acc_bolts,
                   "quantity_rejected": 1000.0 - acc_bolts},
                  {"description": "Bearing 6205ZZ", "quantity_received": 100.0, "quantity_accepted": 100.0}])


def _inv(rate_bolt=12.0, qty_bolt=1000.0, gst=18.0):
    lines = [{"description": "MS Hex Bolt M12x50", "quantity": qty_bolt, "rate": rate_bolt, "gst_rate": gst,
              "taxable_value": qty_bolt * rate_bolt},
             {"description": "Ball Bearing 6205 ZZ", "quantity": 100.0, "rate": 180.0, "gst_rate": 18.0, "taxable_value": 18000.0}]
    return build(INVOICE, {"invoice_number": "INV/0091", "po_reference": "PO/4411", "supplier_gstin": "27AAPFU0939F1ZV",
                           "subtotal": sum(l["taxable_value"] for l in lines)}, lines)


def test_three_way_clean_match():
    r = three_way_match(_po(), [_grn()], _inv())
    assert r.status == "matched", [i.message for i in r.issues]


def test_three_way_flags_price_qty_and_tax():
    r = three_way_match(_po(), [_grn(acc_bolts=950.0)], _inv(rate_bolt=12.5, gst=12.0))
    kinds = {i.kind for i in r.issues}
    assert r.status == "mismatch"
    assert {"price", "quantity", "tax", "total"} <= kinds
    msgs = " ".join(i.message for i in r.issues)
    assert "billed 1000 but only 950 accepted" in msgs and "₹12.50 vs PO rate ₹12.00" in msgs


def test_envelope_encryption_and_tamper_detection():
    blob = crypto.encrypt_blob(b"invoice bytes", b"aad")
    assert b"invoice" not in blob
    assert crypto.decrypt_blob(blob, b"aad") == b"invoice bytes"
    with pytest.raises(Exception):
        crypto.decrypt_blob(blob, b"other-aad")
    tok = crypto.encrypt_str("27AAPFU0939F1ZV")
    assert "27AAPFU" not in tok and crypto.decrypt_str(tok) == "27AAPFU0939F1ZV"
    assert crypto.pseudonymise("27aapfu0939f1zv") == crypto.pseudonymise("27AAPFU0939F1ZV")


def test_audit_rejects_pii_and_detects_tampering():
    with pytest.raises(AuditPIIError):
        record("x", vendor="Shree Ganesh Engineering Works")  # spaces => free text => refused
    record("a.one", count=1)
    record("a.two", count=2)
    assert verify_chain() == (True, None)
    from sereno.db import session_scope
    from sereno.models import AuditEvent
    with session_scope() as s:
        ev = s.query(AuditEvent).filter(AuditEvent.event == "a.one").one()
        ev.meta = {"count": 99}
    ok, bad = verify_chain()
    assert not ok and bad == 1


def test_template_hints_from_examples(tenant_and_users):
    from sqlalchemy import select

    from sereno import jobs, pipeline
    from sereno.db import session_scope
    from sereno.eval.fake_model import TruthResponder
    from sereno.eval.synth import invoice_truth, make_invoice, to_jpeg_bytes
    from sereno.extraction.llm import FakeLLM, set_llm
    from sereno.models import Document, User
    from sereno.templates_onboarding import activate, create_template

    tid, users = tenant_and_users
    rng = random.Random(30)
    base = make_invoice(rng, "good_scan")
    ids = []
    for i in range(3):
        sd = make_invoice(random.Random(100 + i), "good_scan")
        sd.truth["supplier_gstin"] = base.truth["supplier_gstin"]  # same vendor
        set_llm(FakeLLM(TruthResponder(sd)))
        with session_scope() as s:
            ids.append(pipeline.ingest_upload(s, tid, users["uploader"], f"{i}.jpg", to_jpeg_bytes(sd.image), "invoice").document_id)
        jobs.drain()
    with session_scope() as s:
        admin = s.get(User, users["admin"])
        for d in s.execute(select(Document).where(Document.id.in_(ids))).scalars():
            d.status = "ready"
        t = create_template(s, admin, "invoice", "Vendor A format", ids, [{"name": "vendor_code", "label": "Vendor code"}])
        assert "invoice_number" in t.hints and "top right" in t.hints
        activate(s, admin, t)
        assert t.status == "active"
