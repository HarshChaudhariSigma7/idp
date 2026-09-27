"""The app processes documents on parallel worker threads (and the real-model eval uses the same
path). This checks concurrent processing on the dev database: no lost documents, no duplicate
rows, and an intact audit hash chain."""
import io
import json
import random
import time

from PIL import Image

from sereno import jobs
from sereno.eval.fake_model import TruthResponder
from sereno.eval.synth import make_invoice
from sereno.extraction.llm import FakeLLM, set_llm


def _copies(sd, n):
    out = []
    for q in range(n):  # different bytes, same content (distinct uploads, same ground truth)
        buf = io.BytesIO()
        Image.fromarray(sd.image).save(buf, format="JPEG", quality=80 + q)
        out.append(buf.getvalue())
    return out


def test_parallel_eval_path_with_worker_threads(tmp_path, monkeypatch):
    from sereno.eval import run_eval
    sd = make_invoice(random.Random(55), "good_scan")
    ds = tmp_path / "ds" / "good_scan"
    ds.mkdir(parents=True)
    truth = {"doc_type": "invoice", "bucket": "good_scan", "synthetic": True, "fields": sd.truth, "line_items": sd.lines}
    for i, data in enumerate(_copies(sd, 6)):
        (ds / f"d{i}.jpg").write_bytes(data)
        (ds / f"d{i}.truth.json").write_text(json.dumps(truth))
    set_llm(FakeLLM(TruthResponder(sd)))  # shared responder: every copy has the same truth
    monkeypatch.setenv("SERENO_WORKER_POLL_S", "0.1")
    monkeypatch.setenv("SERENO_WORKER_THREADS", "3")
    rep = run_eval.run(tmp_path / "ds", tmp_path / "out", fake=False, workers=3, timeout_s=240)
    assert rep["overall"]["documents"] == 6
    # six copies of one invoice processed in parallel: exactly one is accepted, five are caught as
    # duplicates even though the workers ran at the same time
    statuses = sorted(d["status"] for d in rep["documents"])
    assert statuses == ["needs_review"] * 5 + ["ready"], statuses
    assert rep["overall"]["field_accuracy_pct"] == 100.0

    from sereno.db import session_scope
    from sereno.models import Document, ExtractedField, Page
    from sereno.security.audit import verify_chain
    from sereno.models import ValidationResult
    with session_scope() as s:
        dups = s.query(ValidationResult).filter(ValidationResult.check_id == "duplicate", ValidationResult.status == "fail").count()
        assert dups == 5
        for d in s.query(Document).all():
            assert s.query(Page).filter(Page.document_id == d.id).count() == 1
            assert s.query(ExtractedField).filter(ExtractedField.document_id == d.id).count() == d.fields_total
    assert verify_chain() == (True, None)
    assert not jobs._threads  # workers stopped
