import random

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sereno import jobs
from sereno.db import session_scope
from sereno.eval.fake_model import TruthResponder
from sereno.eval.synth import make_invoice, to_jpeg_bytes
from sereno.extraction.llm import FakeLLM, set_llm
from sereno.models import Document, Tenant, User
from sereno.security.auth import hash_password

H = {"x-sereno": "1"}


@pytest.fixture
def client():
    from sereno.api.app import app
    with TestClient(app) as c:
        yield c


def login(client, email, password="correct horse battery"):
    c = client
    c.cookies.clear()
    r = c.post("/api/auth/login", json={"email": email, "password": password}, headers=H)
    assert r.status_code == 200, r.text
    if not r.json()["mfa_enrolled"]:
        uri = c.post("/api/auth/mfa/enroll", headers=H).json()["otpauth_uri"]
        secret = pyotp.parse_uri(uri).secret
    else:
        with session_scope() as s:
            secret = s.execute(select(User).where(User.email == email)).scalar_one().mfa_secret
    r = c.post("/api/auth/mfa/verify", json={"code": pyotp.TOTP(secret).now()}, headers=H)
    assert r.status_code == 200, r.text
    return c


def upload(client, sd):
    set_llm(FakeLLM(TruthResponder(sd, errors={"primary": {"line_items[0].quantity": 7.0}})))
    r = client.post("/api/documents", files={"files": ("inv.jpg", to_jpeg_bytes(sd.image), "image/jpeg")},
                    data={"doc_type": "invoice"}, headers=H)
    assert r.status_code == 200, r.text
    jobs.drain()
    return r.json()["results"][0]["document_id"]


def test_mfa_is_mandatory(client, tenant_and_users):
    r = client.post("/api/auth/login", json={"email": "manager@sahyadri.test", "password": "correct horse battery"}, headers=H)
    assert r.status_code == 200
    assert client.get("/api/documents").status_code == 401  # password alone is not enough
    r = client.post("/api/auth/mfa/verify", json={"code": "000000"}, headers=H)
    assert r.status_code == 401


def test_csrf_header_required(client, tenant_and_users):
    r = client.post("/api/auth/login", json={"email": "manager@sahyadri.test", "password": "x"})
    assert r.status_code == 403


def test_lockout_after_failed_logins(client, tenant_and_users):
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "admin@sahyadri.test", "password": "wrong password!!"}, headers=H)
    r = client.post("/api/auth/login", json={"email": "admin@sahyadri.test", "password": "correct horse battery"}, headers=H)
    assert r.status_code == 401 and "locked" in r.json()["detail"]


def test_end_to_end_review_export_and_rbac(client, tenant_and_users):
    tid, users = tenant_and_users
    login(client, "uploader@sahyadri.test")
    sd = make_invoice(random.Random(21), "good_scan")
    doc_id = upload(client, sd)
    assert client.get("/api/dashboard").status_code == 403  # uploader can't see dashboard

    # a second reviewer in the same company must not see a document assigned to someone else
    with session_scope() as s:
        s.add(User(tenant_id=tid, email="other@sahyadri.test", name="Other", role="reviewer",
                   password_hash=hash_password("correct horse battery")))
        d = s.get(Document, doc_id)
        assert d.assigned_to == users["reviewer"]
    login(client, "other@sahyadri.test")
    assert client.get(f"/api/documents/{doc_id}").status_code == 404
    assert client.get("/api/review/queue").json()["documents"] == []

    # another tenant's admin must not see it either
    with session_scope() as s:
        t2 = Tenant(name="Other Co")
        s.add(t2)
        s.flush()
        s.add(User(tenant_id=t2.id, email="admin@other.test", name="A", role="admin",
                   password_hash=hash_password("correct horse battery")))
    login(client, "admin@other.test")
    assert client.get(f"/api/documents/{doc_id}").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/pages/1").status_code == 404

    # assigned reviewer works only the flagged field
    login(client, "reviewer@sahyadri.test")
    q = client.get("/api/review/queue").json()["documents"]
    assert [x["id"] for x in q] == [doc_id]
    doc = client.get(f"/api/documents/{doc_id}").json()
    assert client.get(f"/api/documents/{doc_id}/pages/1").headers["content-type"] == "image/jpeg"
    flagged = [f for f in doc["fields"] if f["needs_review"]]
    assert [f["key"] for f in flagged] == ["line_items[0].quantity"]
    assert "Needs your review" in doc["message"]
    r = client.post(f"/api/review/{doc_id}/complete", json={}, headers=H)
    assert r.status_code == 400  # can't complete with pending fields
    bad = client.post(f"/api/review/{doc_id}/fields/{flagged[0]['id']}", json={"action": "correct", "value": "abc"}, headers=H)
    assert bad.status_code == 400
    r = client.post(f"/api/review/{doc_id}/fields/{flagged[0]['id']}",
                    json={"action": "correct", "value": str(sd.lines[0]["quantity"]), "time_ms": 3100}, headers=H)
    assert r.status_code == 200 and r.json()["failed_checks"] == []
    r = client.post(f"/api/review/{doc_id}/complete", json={}, headers=H)
    assert r.json()["completed"] is True

    # manager dashboard + export
    login(client, "manager@sahyadri.test")
    dash = client.get("/api/dashboard").json()
    assert dash["turnaround"]["reviewed"] == 1 and dash["accuracy"]["fields_extracted"] > 0
    conds = {c["key"]: c for c in dash["conditions"]}
    assert set(conds) == {"overall", "printed", "vernacular", "handwritten"}
    assert conds["overall"]["fields"] > 0 and not conds["overall"]["measured"]  # < 200 fields: no claim
    assert conds["overall"]["caught_before_export_pct"] == 100.0  # the one error was flagged, not exported
    steps = [t["step"] for t in dash["technology"]]
    assert steps[0] == "Quality pre-check" and "Machine-readable codes" in steps and len(steps) == 8
    r = client.get("/api/exports", params={"doc_type": "invoice", "fmt": "csv"})
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert sd.truth["invoice_number"] in text and f"{sd.lines[0]['quantity']}" in text
    assert client.get(f"/api/documents/{doc_id}").json()["status"] == "exported"
    r = client.get("/api/exports", params={"doc_type": "invoice", "fmt": "xlsx", "include_exported": True})
    assert r.status_code == 200 and r.content[:2] == b"PK"

    # audit trail is visible to admin and contains the reviewer's correction event, no values
    login(client, "admin@sahyadri.test")
    events = client.get("/api/admin/audit").json()["events"]
    names = {e["event"] for e in events}
    assert {"document.uploaded", "document.extracted", "review.field_correct", "review.document_completed",
            "export.created", "auth.login"} <= names
    assert sd.truth["invoice_number"] not in str(events)
    assert client.get("/api/subprocessors").json()["subprocessors"][0]["name"].startswith("Anthropic")


def test_internal_metrics_only_for_sereno_ops(client, tenant_and_users):
    with session_scope() as s:
        s.add(User(tenant_id=None, email="ops@sereno.test", name="Ops", role="sereno_ops",
                   password_hash=hash_password("correct horse battery")))
    login(client, "ops@sereno.test")
    r = client.get("/api/internal/metrics")
    assert r.status_code == 200 and "by_quality_bucket" in r.json()
    assert client.get("/api/documents").json()["documents"] == []  # no document content for ops
    login(client, "admin@sahyadri.test")
    assert client.get("/api/internal/metrics").status_code == 403


def test_upload_refused_plainly_without_ai_key(client, tenant_and_users, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", "/nonexistent-home")
    set_llm(None)
    login(client, "uploader@sahyadri.test")
    assert client.get("/api/me").json()["ai_ready"] is False
    r = client.post("/api/documents", files={"files": ("x.jpg", b"\xff\xd8\xff" + b"0" * 100, "image/jpeg")}, headers=H)
    assert "No Anthropic API key" in r.json()["results"][0]["error"]
    with session_scope() as s:
        assert s.query(Document).count() == 0
