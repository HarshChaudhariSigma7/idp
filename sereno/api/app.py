from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from sereno import jobs, pipeline  # noqa: F401  (registers job handlers)
from sereno.api.deps import COOKIE, current_user, get_db, pre_mfa_user, require
from sereno.config import get_settings
from sereno.db import init_db
from sereno.exports.tabular import rows_for, to_csv, to_xlsx
from sereno.extraction.doc_specs import SPECS
from sereno.extraction.llm import ai_ready
from sereno.ingest.loader import UnsupportedDocument
from sereno.matching.three_way import Tolerances, three_way_match
from sereno.metrics import dashboard, internal_metrics
from sereno.models import AuditEvent, Document, ExtractedField, MatchSet, Template, Tenant, User, ValidationResult, utcnow
from sereno.review import ReviewError, act_on_field, complete_document, effective_value, rebuild_extracted, recheck
from sereno.routing import STATUS_LABELS
from sereno.security import audit
from sereno.security.auth import (AuthError, authenticate, begin_mfa_enrollment, create_session, hash_password,
                                  revoke_session, verify_totp)
from sereno.security.rbac import ROLES, can, can_access_document, can_review_document, scope_documents
from sereno.storage.temp_store import DOCS, BlobGone, get_store
from sereno.templates_onboarding import TemplateError, activate, create_template

log = logging.getLogger("sereno.api")
WEB = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    st = get_settings()
    if st.run_worker_in_app and not st.is_test:
        jobs.reset_stale()
        jobs.start_workers()
    yield
    jobs.stop_workers()


app = FastAPI(title="Sereno Volante", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
        # CSRF: browsers cannot send this custom header cross-site without a CORS preflight we never allow.
        if request.headers.get("x-sereno") != "1":
            return JSONResponse({"detail": "Missing request header"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
                                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
    if get_settings().cookie_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# --- auth ----------------------------------------------------------------------------------------

class LoginIn(BaseModel):
    email: str
    password: str


class CodeIn(BaseModel):
    code: str


def _set_cookie(resp: Response, token: str) -> None:
    st = get_settings()
    resp.set_cookie(COOKIE, token, httponly=True, secure=st.cookie_secure, samesite="strict",
                    max_age=st.session_hours * 3600, path="/")


@app.post("/api/auth/login")
def login(body: LoginIn, response: Response, s: Session = Depends(get_db)):
    try:
        user = authenticate(s, body.email, body.password)
    except AuthError as e:
        s.commit()  # persist failed-attempt counter
        audit.record("auth.login_failed")
        raise HTTPException(401, str(e))
    token = create_session(s, user)
    _set_cookie(response, token)
    audit.record("auth.password_ok", session=s, tenant_id=user.tenant_id, actor_id=user.id)
    return {"mfa_enrolled": user.mfa_enabled}


@app.post("/api/auth/mfa/enroll")
def mfa_enroll(ctx=Depends(pre_mfa_user), s: Session = Depends(get_db)):
    sess, user = ctx
    if user.mfa_enabled:
        raise HTTPException(400, "Two-factor is already set up")
    uri, svg = begin_mfa_enrollment(user)
    return {"otpauth_uri": uri, "qr_svg": svg}


@app.post("/api/auth/mfa/verify")
def mfa_verify(body: CodeIn, ctx=Depends(pre_mfa_user), s: Session = Depends(get_db)):
    sess, user = ctx
    if not verify_totp(user, body.code):
        audit.record("auth.mfa_failed", tenant_id=user.tenant_id, actor_id=user.id)
        raise HTTPException(401, "That code didn't match. Check your authenticator app and try again.")
    if not user.mfa_enabled:
        user.mfa_enabled = True
        audit.record("auth.mfa_enrolled", session=s, tenant_id=user.tenant_id, actor_id=user.id)
    sess.mfa_passed = True
    user.last_login_at = utcnow()
    audit.record("auth.login", session=s, tenant_id=user.tenant_id, actor_id=user.id)
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, s: Session = Depends(get_db)):
    tok = request.cookies.get(COOKIE)
    if tok:
        revoke_session(s, tok)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user), s: Session = Depends(get_db)):
    tenant = s.get(Tenant, user.tenant_id) if user.tenant_id else None
    perms = [p for p in ("document.upload", "document.view_all", "review.work", "dashboard.view", "export.run",
                         "match.run", "template.manage", "user.manage", "audit.view", "metrics.internal") if can(user.role, p)]
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role,
            "company": tenant.name if tenant else "Sereno Volante", "permissions": perms,
            "ai_ready": ai_ready(), "demo": bool(tenant and (tenant.settings or {}).get("demo")),
            "sla_minutes": get_settings().review_sla_minutes, "doc_types": {k: v.label for k, v in SPECS.items()}}


# --- documents -----------------------------------------------------------------------------------

def _doc_summary(d: Document) -> dict:
    return {"id": d.id, "filename": d.filename, "doc_type": d.doc_type, "doc_type_label": SPECS[d.doc_type].label if d.doc_type in SPECS else None,
            "status": d.status, "status_label": STATUS_LABELS.get(d.status, d.status), "message": d.status_message,
            "created_at": d.created_at, "processed_at": d.processed_at, "review_due_at": d.review_due_at,
            "reviewed_at": d.reviewed_at, "fields_flagged": d.fields_flagged, "fields_total": d.fields_total,
            "page_count": d.page_count, "quality": d.quality_bucket, "assigned_to": d.assigned_to,
            "is_qa_sample": d.is_qa_sample, "available": d.blob_purged_at is None}


def _get_doc(s: Session, user: User, doc_id: str) -> Document:
    d = s.get(Document, doc_id)
    if d is None or not can_access_document(user, d):
        raise HTTPException(404, "Document not found")
    return d


@app.post("/api/documents")
async def upload(files: list[UploadFile] = File(...), doc_type: str = Form("auto"),
                 user: User = Depends(require("document.upload")), s: Session = Depends(get_db)):
    if not ai_ready():
        backend = get_settings().llm_backend
        key_name = "GEMINI_API_KEY" if backend == "gemini" else "ANTHROPIC_API_KEY"
        msg = (f"No {key_name} is configured on this server, so new documents can't be read yet. "
               f"Add {key_name} to .env and restart.")
        return {"results": [{"filename": f.filename, "error": msg} for f in files[:50]]}
    out = []
    for f in files[:50]:
        data = await f.read()
        try:
            r = pipeline.ingest_upload(s, user.tenant_id, user.id, f.filename or "upload", data, doc_type)
            out.append({"filename": f.filename, "document_id": r.document_id, "duplicate": r.duplicate})
        except UnsupportedDocument as e:
            out.append({"filename": f.filename, "error": str(e)})
    return {"results": out}


@app.get("/api/documents")
def list_documents(status: str | None = None, doc_type: str | None = None, q_limit: int = Query(200, le=500),
                   user: User = Depends(current_user), s: Session = Depends(get_db)):
    q = scope_documents(s.query(Document), user)
    if status:
        q = q.filter(Document.status.in_(status.split(",")))
    if doc_type:
        q = q.filter(Document.doc_type == doc_type)
    return {"documents": [_doc_summary(d) for d in q.order_by(Document.created_at.desc()).limit(q_limit)]}


def _field_out(f: ExtractedField) -> dict:
    return {"id": f.id, "key": f.key, "name": f.field_name, "label": f.label, "group": f.group, "line_index": f.line_index,
            "type": f.value_type, "high_stakes": f.is_high_stakes, "value": effective_value(f), "extracted_value": f.value,
            "raw_text": f.raw_text, "alt_value": f.alt_value, "page": f.page, "bbox": f.bbox, "band": f.band,
            "needs_review": f.needs_review, "reason": f.review_reason, "status": f.status,
            "suggested_value": f.suggested_value, "suggestion_reason": f.suggestion_reason, "evidence": f.evidence or []}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, user: User = Depends(current_user), s: Session = Depends(get_db)):
    d = _get_doc(s, user, doc_id)
    checks = s.execute(select(ValidationResult).where(ValidationResult.document_id == d.id,
                                                      ValidationResult.stage == "extracted")).scalars().all()
    audit.record("document.viewed", session=s, tenant_id=d.tenant_id, actor_id=user.id, object_type="document", object_id=d.id)
    return {**_doc_summary(d), "can_review": can_review_document(user, d) and d.reviewed_at is None,
            "pages": [{"page_no": p.page_no, "width": p.width, "height": p.height, "corrections": p.corrections_applied}
                      for p in d.pages],
            "fields": [_field_out(f) for f in d.fields],
            "checks": [{"id": c.check_id, "status": c.status, "severity": c.severity, "message": c.message,
                        "field_keys": c.field_keys} for c in checks],
            "languages": d.languages, "handwriting": d.has_handwriting, "evidence_summary": d.evidence_summary or {},
            "parent_id": d.parent_id}


@app.get("/api/documents/{doc_id}/pages/{page_no}")
def page_image(doc_id: str, page_no: int, user: User = Depends(current_user), s: Session = Depends(get_db)):
    d = _get_doc(s, user, doc_id)
    try:
        data = get_store().get(DOCS, d.id, f"page-{page_no}")
    except BlobGone:
        raise HTTPException(410, "This document image was deleted under the retention policy")
    audit.record("document.page_viewed", session=s, tenant_id=d.tenant_id, actor_id=user.id, object_type="document",
                 object_id=d.id, page=page_no)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@app.post("/api/documents/{doc_id}/retry")
def retry(doc_id: str, user: User = Depends(require("document.upload")), s: Session = Depends(get_db)):
    d = _get_doc(s, user, doc_id)
    if d.status != "failed":
        raise HTTPException(400, "Only failed documents can be retried")
    d.status = "uploaded"
    jobs.enqueue(s, "process_document", {"document_id": d.id})
    audit.record("document.retry", session=s, tenant_id=d.tenant_id, actor_id=user.id, object_type="document", object_id=d.id)
    return {"ok": True}


# --- review --------------------------------------------------------------------------------------

@app.get("/api/review/queue")
def review_queue(user: User = Depends(require("review.work")), s: Session = Depends(get_db)):
    q = scope_documents(s.query(Document), user).filter(Document.reviewed_at.is_(None), Document.review_entered_at.is_not(None))
    if user.role in ("admin", "manager"):
        pass  # managers see the whole company queue
    # flagged documents first (spot-checked or not); pure spot checks of auto-approved documents last
    docs = q.order_by(and_(Document.is_qa_sample, Document.fields_flagged == 0), Document.review_due_at).limit(300).all()
    now = utcnow()
    return {"now": now, "documents": [{**_doc_summary(d), "overdue": bool(d.review_due_at and d.review_due_at < now)}
                                      for d in docs]}


class FieldAction(BaseModel):
    action: str = Field(pattern="^(confirm|correct)$")
    value: str | float | int | None = None
    time_ms: int | None = None


@app.post("/api/review/{doc_id}/fields/{field_id}")
def review_field(doc_id: str, field_id: str, body: FieldAction, user: User = Depends(require("review.work")),
                 s: Session = Depends(get_db)):
    d = _get_doc(s, user, doc_id)
    if not can_review_document(user, d):
        raise HTTPException(403, "This document isn't assigned to you")
    f = s.get(ExtractedField, field_id)
    if f is None:
        raise HTTPException(404, "Field not found")
    try:
        act_on_field(s, user, d, f, body.action, body.value, body.time_ms)
    except ReviewError as e:
        raise HTTPException(400, str(e))
    return {"field": _field_out(f), "failed_checks": recheck(s, d)}


class CompleteIn(BaseModel):
    override_failed_checks: bool = False


@app.post("/api/review/{doc_id}/complete")
def review_complete(doc_id: str, body: CompleteIn, user: User = Depends(require("review.work")), s: Session = Depends(get_db)):
    d = _get_doc(s, user, doc_id)
    if not can_review_document(user, d):
        raise HTTPException(403, "This document isn't assigned to you")
    try:
        return complete_document(s, user, d, body.override_failed_checks)
    except ReviewError as e:
        raise HTTPException(400, str(e))


class AssignIn(BaseModel):
    user_id: str


@app.post("/api/review/{doc_id}/assign")
def review_assign(doc_id: str, body: AssignIn, user: User = Depends(require("review.assign")), s: Session = Depends(get_db)):
    d = _get_doc(s, user, doc_id)
    target = s.get(User, body.user_id)
    if target is None or not can(target.role, "review.work") or target.tenant_id not in (None, d.tenant_id):
        raise HTTPException(400, "That person can't review documents")
    if target.tenant_id is None and not s.get(Tenant, d.tenant_id).managed_review:
        raise HTTPException(400, "Managed review is not enabled for your company")
    d.assigned_to = target.id
    audit.record("review.assigned", session=s, tenant_id=d.tenant_id, actor_id=user.id, object_type="document", object_id=d.id,
                 assignee=target.id)
    return {"ok": True}


# --- dashboard & metrics -------------------------------------------------------------------------

@app.get("/api/dashboard")
def get_dashboard(days: int = Query(30, ge=1, le=365), user: User = Depends(require("dashboard.view")),
                  s: Session = Depends(get_db)):
    return dashboard(s, user.tenant_id, days)


@app.get("/api/internal/metrics")
def get_internal(days: int = Query(30, ge=1, le=365), user: User = Depends(require("metrics.internal")),
                 s: Session = Depends(get_db)):
    return internal_metrics(s, days)


# --- exports -------------------------------------------------------------------------------------

@app.get("/api/exports")
def export(doc_type: str, fmt: str = Query("xlsx", pattern="^(csv|xlsx)$"), ids: str | None = None,
           include_exported: bool = False, user: User = Depends(require("export.run")), s: Session = Depends(get_db)):
    if doc_type not in SPECS:
        raise HTTPException(400, "Unknown document type")
    statuses = ["ready", "exported"] if include_exported else ["ready"]
    q = scope_documents(s.query(Document), user).filter(Document.doc_type == doc_type, Document.status.in_(statuses))
    if ids:
        q = q.filter(Document.id.in_(ids.split(",")))
    docs = q.order_by(Document.created_at).all()
    if not docs:
        raise HTTPException(404, "No documents ready to export")
    tenant = s.get(Tenant, user.tenant_id)
    profile = ((tenant.settings or {}).get("export_profiles") or {}).get(doc_type)
    now = utcnow()
    for d in docs:
        d.exported_at, d.status = now, "exported"
    audit.record("export.created", session=s, tenant_id=user.tenant_id, actor_id=user.id, doc_type=doc_type, fmt=fmt, count=len(docs))
    name = f"sereno_{doc_type}_{now:%Y%m%d_%H%M}"
    if fmt == "csv":
        cols, rows = rows_for(docs, doc_type, profile, "lines")
        return Response(to_csv(cols, rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    sheets = {"Documents": rows_for(docs, doc_type, profile, "header")}
    if SPECS[doc_type].line_fields:
        sheets["Line items"] = rows_for(docs, doc_type, profile, "lines")
    return Response(to_xlsx(sheets), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})


# --- 3-way match ---------------------------------------------------------------------------------

class MatchIn(BaseModel):
    po_id: str
    grn_ids: list[str] = []
    invoice_id: str


@app.post("/api/match")
def run_match(body: MatchIn, user: User = Depends(require("match.run")), s: Session = Depends(get_db)):
    po = _get_doc(s, user, body.po_id)
    inv = _get_doc(s, user, body.invoice_id)
    grns = [_get_doc(s, user, g) for g in body.grn_ids]
    if po.doc_type != "po" or inv.doc_type != "invoice" or any(g.doc_type != "grn" for g in grns):
        raise HTTPException(400, "Pick one purchase order, its goods receipt notes, and one invoice")
    pending = [d for d in [po, inv, *grns] if d.status not in ("ready", "exported")]
    if pending:
        raise HTTPException(400, "All documents must be reviewed and ready before matching")
    tenant = s.get(Tenant, user.tenant_id)
    tol = Tolerances(**((tenant.settings or {}).get("match_tolerances") or {}))
    res = three_way_match(rebuild_extracted(s, po), [rebuild_extracted(s, g) for g in grns], rebuild_extracted(s, inv), tol)
    m = MatchSet(tenant_id=user.tenant_id, po_document_id=po.id, grn_document_ids=[g.id for g in grns],
                 invoice_document_id=inv.id, status=res.status, summary=res.summary, result=res.as_dict(), created_by=user.id)
    s.add(m)
    s.flush()
    audit.record("match.run", session=s, tenant_id=user.tenant_id, actor_id=user.id, object_type="match", object_id=m.id,
                 status=res.status, issues=len(res.issues))
    return {"id": m.id, **res.as_dict()}


@app.get("/api/match")
def list_matches(user: User = Depends(require("match.run")), s: Session = Depends(get_db)):
    rows = s.execute(select(MatchSet).where(MatchSet.tenant_id == user.tenant_id).order_by(MatchSet.created_at.desc())
                     .limit(100)).scalars().all()
    return {"matches": [{"id": m.id, "status": m.status, "summary": m.summary, "created_at": m.created_at,
                         "po_id": m.po_document_id, "invoice_id": m.invoice_document_id, "grn_ids": m.grn_document_ids}
                        for m in rows]}


@app.get("/api/match/{match_id}")
def get_match(match_id: str, user: User = Depends(require("match.run")), s: Session = Depends(get_db)):
    m = s.get(MatchSet, match_id)
    if m is None or m.tenant_id != user.tenant_id:
        raise HTTPException(404, "Not found")
    return {"id": m.id, **m.result}


# --- templates -----------------------------------------------------------------------------------

class TemplateIn(BaseModel):
    doc_type: str
    name: str
    example_document_ids: list[str]
    extra_fields: list[dict] = []


@app.get("/api/templates")
def list_templates(user: User = Depends(require("template.manage")), s: Session = Depends(get_db)):
    rows = s.execute(select(Template).where(Template.tenant_id == user.tenant_id).order_by(Template.created_at.desc())).scalars()
    return {"templates": [{"id": t.id, "name": t.name, "doc_type": t.doc_type, "status": t.status, "hints": t.hints,
                           "extra_fields": t.extra_fields, "examples": len(t.example_document_ids),
                           "created_at": t.created_at} for t in rows]}


@app.post("/api/templates")
def new_template(body: TemplateIn, user: User = Depends(require("template.manage")), s: Session = Depends(get_db)):
    if body.doc_type not in SPECS:
        raise HTTPException(400, "Unknown document type")
    try:
        t = create_template(s, user, body.doc_type, body.name, body.example_document_ids, body.extra_fields)
    except TemplateError as e:
        raise HTTPException(400, str(e))
    return {"id": t.id, "hints": t.hints, "status": t.status}


class TemplateUpdate(BaseModel):
    hints: str | None = None
    status: str | None = Field(default=None, pattern="^(active|retired)$")


@app.post("/api/templates/{tid}")
def update_template(tid: str, body: TemplateUpdate, user: User = Depends(require("template.manage")),
                    s: Session = Depends(get_db)):
    t = s.get(Template, tid)
    if t is None or t.tenant_id != user.tenant_id:
        raise HTTPException(404, "Not found")
    if body.hints is not None:
        t.hints = body.hints[:5000]
        audit.record("template.hints_edited", session=s, tenant_id=t.tenant_id, actor_id=user.id, object_type="template", object_id=t.id)
    try:
        if body.status == "active":
            activate(s, user, t)
        elif body.status == "retired":
            t.status = "retired"
    except TemplateError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": t.status}


# --- admin ---------------------------------------------------------------------------------------

class UserIn(BaseModel):
    email: str
    name: str
    role: str
    password: str


@app.get("/api/admin/users")
def list_users(user: User = Depends(require("user.manage")), s: Session = Depends(get_db)):
    rows = s.execute(select(User).where(User.tenant_id == user.tenant_id).order_by(User.created_at)).scalars()
    return {"users": [{"id": u.id, "email": u.email, "name": u.name, "role": u.role, "active": u.is_active,
                       "mfa": u.mfa_enabled, "last_login_at": u.last_login_at} for u in rows],
            "roles": {k: v for k, v in ROLES.items() if not k.startswith("sereno")}}


@app.post("/api/admin/users")
def create_user(body: UserIn, user: User = Depends(require("user.manage")), s: Session = Depends(get_db)):
    if body.role not in ROLES or body.role.startswith("sereno"):
        raise HTTPException(400, "Unknown role")
    email = body.email.strip().lower()
    if s.execute(select(User).where(User.email == email)).scalar_one_or_none():
        raise HTTPException(400, "A user with that email already exists")
    try:
        ph = hash_password(body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    u = User(tenant_id=user.tenant_id, email=email, name=body.name[:200], role=body.role, password_hash=ph)
    s.add(u)
    s.flush()
    audit.record("user.created", session=s, tenant_id=user.tenant_id, actor_id=user.id, object_type="user", object_id=u.id, role=body.role)
    return {"id": u.id}


class UserPatch(BaseModel):
    active: bool | None = None
    role: str | None = None


@app.post("/api/admin/users/{uid}")
def update_user(uid: str, body: UserPatch, user: User = Depends(require("user.manage")), s: Session = Depends(get_db)):
    u = s.get(User, uid)
    if u is None or u.tenant_id != user.tenant_id:
        raise HTTPException(404, "Not found")
    if body.active is not None:
        u.is_active = body.active
    if body.role is not None:
        if body.role not in ROLES or body.role.startswith("sereno"):
            raise HTTPException(400, "Unknown role")
        u.role = body.role
    audit.record("user.updated", session=s, tenant_id=user.tenant_id, actor_id=user.id, object_type="user", object_id=u.id,
                 active=u.is_active, role=u.role)
    return {"ok": True}


@app.get("/api/admin/audit")
def get_audit(limit: int = Query(200, le=1000), user: User = Depends(require("audit.view")), s: Session = Depends(get_db)):
    rows = s.execute(select(AuditEvent).where(AuditEvent.tenant_id == user.tenant_id).order_by(AuditEvent.seq.desc())
                     .limit(limit)).scalars()
    return {"events": [{"seq": e.seq, "ts": e.ts, "event": e.event, "actor_id": e.actor_id, "object_type": e.object_type,
                        "object_id": e.object_id, "meta": e.meta} for e in rows]}


class CompanyIn(BaseModel):
    own_gstins: list[str] = []


@app.get("/api/admin/company")
def get_company(user: User = Depends(require("user.manage")), s: Session = Depends(get_db)):
    from sereno.masterdata import LEARN_OWN_AFTER
    st = s.get(Tenant, user.tenant_id).settings or {}
    learned = sorted(g for g, n in (st.get("buyer_gstin_counts") or {}).items() if n >= LEARN_OWN_AFTER)
    return {"own_gstins": st.get("own_gstins", []), "learned_gstins": learned}


@app.post("/api/admin/company")
def set_company(body: CompanyIn, user: User = Depends(require("user.manage")), s: Session = Depends(get_db)):
    from sereno.extraction.normalize import clean_code
    from sereno.validation.gst import gstin_problem
    gstins = sorted({clean_code(g) for g in body.own_gstins if g.strip()})
    bad = [g for g in gstins if gstin_problem(g)]
    if bad:
        raise HTTPException(400, f"Not a valid GSTIN: {', '.join(bad)}")
    t = s.get(Tenant, user.tenant_id)
    t.settings = {**(t.settings or {}), "own_gstins": gstins}
    audit.record("company.gstins_updated", session=s, tenant_id=t.id, actor_id=user.id, count=len(gstins))
    return {"own_gstins": gstins}


@app.get("/api/subprocessors")
def subprocessors(user: User = Depends(current_user)):
    return {"subprocessors": get_settings().subprocessors}


@app.get("/api/health")
def health():
    return {"ok": True}


# --- web app -------------------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})
