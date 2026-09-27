"""System of record. Raw documents never live here (they sit in the time-boxed encrypted blob
store); extracted values and model outputs are stored encrypted at column level."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sereno.db import Base
from sereno.security.crypto import EncryptedJSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Explicit written opt-in required before documents are retained for evaluation.
    eval_retention_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    erp_system: Mapped[str | None] = mapped_column(String(80))  # confirmed with customer, e.g. "Tally Prime"
    managed_review: Mapped[bool] = mapped_column(Boolean, default=True)  # Sereno reviewers work the queue
    settings: Mapped[dict] = mapped_column(JSON, default=dict)  # per-tenant threshold overrides


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), index=True)  # None => Sereno staff
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
    mfa_secret: Mapped[str | None] = mapped_column(EncryptedJSON)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    mfa_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str] = mapped_column(EncryptedJSON)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime_type: Mapped[str] = mapped_column(String(80))
    declared_type: Mapped[str] = mapped_column(String(32), default="auto")
    doc_type: Mapped[str | None] = mapped_column(String(32), index=True)
    # uploaded -> processing -> needs_review | ready | unreadable | failed -> exported
    status: Mapped[str] = mapped_column(String(24), default="uploaded", index=True)
    status_message: Mapped[str | None] = mapped_column(Text)  # plain-language, no PII
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    is_digital: Mapped[bool | None] = mapped_column(Boolean)
    quality: Mapped[dict | None] = mapped_column(JSON)  # numeric signals only
    quality_bucket: Mapped[str | None] = mapped_column(String(24), index=True)
    languages: Mapped[list | None] = mapped_column(JSON)
    has_handwriting: Mapped[bool | None] = mapped_column(Boolean)
    model_used: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    vendor_key: Mapped[str | None] = mapped_column(String(64), index=True)  # HMAC of GSTIN
    template_id: Mapped[str | None] = mapped_column(ForeignKey("templates.id"))
    doc_score: Mapped[float | None] = mapped_column(Float)
    arithmetic_ok: Mapped[bool | None] = mapped_column(Boolean)
    fields_total: Mapped[int] = mapped_column(Integer, default=0)
    fields_flagged: Mapped[int] = mapped_column(Integer, default=0)
    is_qa_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    assigned_to: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    processing_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_entered_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_due_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime)
    blob_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    blob_purged_at: Mapped[datetime | None] = mapped_column(DateTime)
    # HMAC(issuer GSTIN | document number): finds duplicates without storing either in clear
    dedupe_key: Mapped[str | None] = mapped_column(String(64), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), index=True)  # set on documents split from a batch PDF
    evidence_summary: Mapped[dict | None] = mapped_column(JSON)  # counts: qr fields, 3/3 votes, repairs ...

    pages: Mapped[list["Page"]] = relationship(back_populates="document", cascade="all, delete-orphan",
                                               order_by="Page.page_no")
    fields: Mapped[list["ExtractedField"]] = relationship(back_populates="document",
                                                          cascade="all, delete-orphan",
                                                          order_by="ExtractedField.ordinal")


class Page(Base):
    __tablename__ = "pages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    quality: Mapped[dict | None] = mapped_column(JSON)
    corrections_applied: Mapped[list | None] = mapped_column(JSON)
    document: Mapped[Document] = relationship(back_populates="pages")


class ExtractionRun(Base):
    """One model call. Full output is kept (encrypted): this table IS the eval set."""
    __tablename__ = "extraction_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    pass_name: Mapped[str] = mapped_column(String(24))  # triage | primary | secondary | verify
    model: Mapped[str] = mapped_column(String(64))
    served_model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str | None] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    output: Mapped[dict | None] = mapped_column(EncryptedJSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExtractedField(Base):
    __tablename__ = "extracted_fields"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(120))        # e.g. "grand_total", "line_items[2].quantity"
    field_name: Mapped[str] = mapped_column(String(80))   # spec name, e.g. "quantity"
    label: Mapped[str] = mapped_column(String(120))
    group: Mapped[str] = mapped_column(String(40))        # header | parties | amounts | line_items ...
    line_index: Mapped[int | None] = mapped_column(Integer)
    value_type: Mapped[str] = mapped_column(String(16))
    is_high_stakes: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[object | None] = mapped_column(EncryptedJSON)
    raw_text: Mapped[str | None] = mapped_column(EncryptedJSON)
    alt_value: Mapped[object | None] = mapped_column(EncryptedJSON)  # second-pass reading if it disagreed
    page: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[dict | None] = mapped_column(JSON)  # normalised 0..1 {x0,y0,x1,y1}
    score: Mapped[float] = mapped_column(Float, default=0.0)
    band: Mapped[str] = mapped_column(String(12))    # high | medium | low
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_reason: Mapped[str | None] = mapped_column(Text)  # plain language
    reason_codes: Mapped[list | None] = mapped_column(JSON)
    features: Mapped[dict | None] = mapped_column(JSON)  # numeric signals used for scoring (backtest input)
    status: Mapped[str] = mapped_column(String(16), default="auto")  # auto | pending | confirmed | corrected
    final_value: Mapped[object | None] = mapped_column(EncryptedJSON)
    suggested_value: Mapped[object | None] = mapped_column(EncryptedJSON)
    suggestion_reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list | None] = mapped_column(JSON)  # plain tags: "matches e-invoice QR", "3 of 3 readings agree"
    document: Mapped[Document] = relationship(back_populates="fields")


class ValidationResult(Base):
    __tablename__ = "validation_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    check_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(8))  # pass | fail | skip
    severity: Mapped[str] = mapped_column(String(8), default="error")
    message: Mapped[str] = mapped_column(Text)      # plain language
    field_keys: Mapped[list] = mapped_column(JSON, default=list)
    details: Mapped[dict | None] = mapped_column(EncryptedJSON)
    stage: Mapped[str] = mapped_column(String(12), default="extracted")  # extracted | final


class Correction(Base):
    """Every reviewer action, tied to the field and the extraction runs that produced it."""
    __tablename__ = "corrections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    field_id: Mapped[str] = mapped_column(ForeignKey("extracted_fields.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(12))  # confirm | correct
    old_value: Mapped[object | None] = mapped_column(EncryptedJSON)
    new_value: Mapped[object | None] = mapped_column(EncryptedJSON)
    was_flagged: Mapped[bool] = mapped_column(Boolean)
    score_at_review: Mapped[float] = mapped_column(Float)
    time_on_field_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    """Append-only, hash-chained. Never contains raw PII: ids, event types, models, counts only."""
    __tablename__ = "audit_events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    actor_kind: Mapped[str] = mapped_column(String(12))  # user | system
    event: Mapped[str] = mapped_column(String(64), index=True)
    object_type: Mapped[str | None] = mapped_column(String(32))
    object_id: Mapped[str | None] = mapped_column(String(36), index=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(12), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Template(Base):
    """Vendor/document-type template learned from 3-5 reviewed examples. No code required."""
    __tablename__ = "templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    vendor_key: Mapped[str | None] = mapped_column(String(64), index=True)
    hints: Mapped[str | None] = mapped_column(EncryptedJSON)  # layout notes injected into prompts
    extra_fields: Mapped[list] = mapped_column(JSON, default=list)  # [{name,label,type,high_stakes}]
    example_document_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft | active | retired
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VendorFieldStat(Base):
    """Historical accuracy per tenant x vendor x doc type x field, fed by reviewer outcomes."""
    __tablename__ = "vendor_field_stats"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36))
    vendor_key: Mapped[str] = mapped_column(String(64))
    doc_type: Mapped[str] = mapped_column(String(32))
    field_name: Mapped[str] = mapped_column(String(80))
    correct: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (Index("ix_vfs_lookup", "tenant_id", "vendor_key", "doc_type", "field_name", unique=True),)


class MatchSet(Base):
    __tablename__ = "match_sets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    po_document_id: Mapped[str] = mapped_column(String(36))
    grn_document_ids: Mapped[list] = mapped_column(JSON)
    invoice_document_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16))  # matched | mismatch | incomplete
    summary: Mapped[str] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(EncryptedJSON)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ThresholdConfig(Base):
    """Versioned thresholds produced by the weekly backtest. Latest active row wins."""
    __tablename__ = "threshold_configs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    doc_type: Mapped[str] = mapped_column(String(32))
    auto_accept: Mapped[float] = mapped_column(Float)
    high_stakes: Mapped[float] = mapped_column(Float)
    weights: Mapped[dict | None] = mapped_column(JSON)
    evidence: Mapped[dict] = mapped_column(JSON)  # sample sizes, escape rates at chosen point
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VendorProfile(Base):
    """What a vendor's reviewed documents have looked like: the baseline for spotting misread
    GSTINs, unusual invoice-number formats and changed bank accounts (a classic fraud pattern)."""
    __tablename__ = "vendor_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    vendor_key: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str | None] = mapped_column(EncryptedJSON)
    bank_account: Mapped[str | None] = mapped_column(EncryptedJSON)
    invoice_masks: Mapped[dict] = mapped_column(JSON, default=dict)
    docs: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (Index("ix_vendor_profile_lookup", "tenant_id", "vendor_key", unique=True),)
