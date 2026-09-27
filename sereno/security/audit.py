"""Tamper-evident audit log.

Each event stores sha256(prev_hash + canonical event). Editing or deleting any row breaks the
chain, which verify_chain() detects. The meta sanitiser rejects anything that could be PII:
only numbers, booleans and short identifier-like strings (ids, model names, statuses) pass.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading

from sqlalchemy import select, text

from sereno.db import session_scope
from sereno.models import AuditEvent, utcnow

_SAFE_STR = re.compile(r"^[A-Za-z0-9_.:\-/]{0,80}$")
_lock = threading.Lock()
GENESIS = "0" * 64


class AuditPIIError(ValueError):
    pass


def _sanitise(meta: dict) -> dict:
    out = {}
    for k, v in (meta or {}).items():
        if not _SAFE_STR.match(str(k)):
            raise AuditPIIError(f"unsafe audit key {k!r}")
        if v is None or isinstance(v, (bool, int, float)):
            out[k] = v
        elif isinstance(v, str) and _SAFE_STR.match(v):
            out[k] = v
        elif isinstance(v, list) and all(isinstance(i, (int, float)) or
                                         (isinstance(i, str) and _SAFE_STR.match(i)) for i in v):
            out[k] = v
        else:
            raise AuditPIIError(f"audit meta value for {k!r} is not an id/number/status; refusing to log")
    return out


def _digest(prev: str, ev: dict) -> str:
    canon = json.dumps(ev, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + canon).encode()).hexdigest()


def record(event: str, *, session=None, tenant_id: str | None = None, actor_id: str | None = None,
           object_type: str | None = None, object_id: str | None = None, **meta) -> None:
    """Write an audit event. Pass the caller's `session` so the event commits atomically with the
    action it describes. On Postgres an advisory transaction lock serialises chain appends."""
    clean = _sanitise(meta)
    if session is not None:
        _append(session, event, tenant_id, actor_id, object_type, object_id, clean)
        return
    with _lock, session_scope() as s:
        _append(s, event, tenant_id, actor_id, object_type, object_id, clean)


def _append(s, event, tenant_id, actor_id, object_type, object_id, clean) -> None:
    if s.bind.dialect.name == "postgresql":
        s.execute(text("SELECT pg_advisory_xact_lock(424242)"))
    last = s.execute(select(AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1)).scalar()
    prev = last or GENESIS
    ts = utcnow()
    body = {"ts": ts.isoformat(), "tenant_id": tenant_id, "actor_id": actor_id,
            "actor_kind": "user" if actor_id else "system", "event": event,
            "object_type": object_type, "object_id": object_id, "meta": clean}
    s.add(AuditEvent(ts=ts, tenant_id=tenant_id, actor_id=actor_id, actor_kind=body["actor_kind"],
                     event=event, object_type=object_type, object_id=object_id, meta=clean,
                     prev_hash=prev, hash=_digest(prev, body)))
    s.flush()


def verify_chain() -> tuple[bool, int | None]:
    """Returns (ok, first_bad_seq)."""
    with session_scope() as s:
        prev = GENESIS
        for ev in s.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars():
            body = {"ts": ev.ts.isoformat(), "tenant_id": ev.tenant_id, "actor_id": ev.actor_id,
                    "actor_kind": ev.actor_kind, "event": ev.event, "object_type": ev.object_type,
                    "object_id": ev.object_id, "meta": ev.meta}
            if ev.prev_hash != prev or ev.hash != _digest(prev, body):
                return False, ev.seq
            prev = ev.hash
    return True, None
