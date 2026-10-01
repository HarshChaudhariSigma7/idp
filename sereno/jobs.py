"""Postgres-backed job queue (SKIP LOCKED); serialised claim on SQLite for dev. No extra infra."""
from __future__ import annotations

import logging
import socket
import threading
import time
import traceback
from datetime import timedelta
from typing import Callable

from sqlalchemy import select, text

from sereno.config import get_settings
from sereno.db import session_scope
from sereno.models import Job, utcnow

log = logging.getLogger("sereno.jobs")
_claim_lock = threading.Lock()
HANDLERS: dict[str, Callable[[dict], None]] = {}
MAX_ATTEMPTS = 3


class PermanentJobError(Exception):
    """Do not retry (e.g. unsupported file)."""


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(s, kind: str, payload: dict, delay_s: float = 0) -> Job:
    j = Job(kind=kind, payload=payload, run_after=utcnow() + timedelta(seconds=delay_s))
    s.add(j)
    s.flush()
    return j


def claim(worker_id: str) -> Job | None:
    with _claim_lock, session_scope() as s:
        q = select(Job).where(Job.status == "queued", Job.run_after <= utcnow()).order_by(Job.created_at).limit(1)
        if s.bind.dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)
        job = s.execute(q).scalar_one_or_none()
        if job is None:
            return None
        job.status, job.locked_by, job.locked_at = "running", worker_id, utcnow()
        job.attempts += 1
        s.flush()
        s.expunge(job)
        return job


def run_one(worker_id: str = "inline") -> bool:
    job = claim(worker_id)
    if job is None:
        return False
    fn = HANDLERS.get(job.kind)
    try:
        if fn is None:
            raise PermanentJobError(f"no handler for {job.kind}")
        fn(job.payload)
        status, err, retry = "done", None, False
    except PermanentJobError as e:
        status, err, retry = "failed", str(e), False
    except Exception as e:  # noqa: BLE001
        # One line on the terminal by default (the library traceback underneath a network error
        # is rarely what a local run needs to see); the full trace is still one `--log-level
        # debug` away for real debugging.
        log.error("job %s (%s) failed: %s: %s", job.id, job.kind, type(e).__name__, e)
        log.debug("full traceback for job %s:\n%s", job.id, traceback.format_exc())
        retry = job.attempts < MAX_ATTEMPTS
        status, err = ("queued" if retry else "failed"), f"{type(e).__name__}: {e}"[:2000]
    with session_scope() as s:
        j = s.get(Job, job.id)
        j.status, j.last_error, j.locked_by = status, err, None
        if retry:
            j.run_after = utcnow() + timedelta(seconds=30 * 2 ** (job.attempts - 1))
    if status == "failed":
        on_failed = HANDLERS.get(f"{job.kind}:failed")
        if on_failed:
            on_failed({**job.payload, "error": err or ""})
    return True


def drain(max_jobs: int = 1000) -> int:
    """Process queued jobs inline until none are runnable (tests, CLI)."""
    n = 0
    while n < max_jobs and run_one():
        n += 1
    return n


_stop = threading.Event()
_threads: list[threading.Thread] = []


def _loop(worker_id: str, periodic: bool):
    st = get_settings()
    last_periodic = 0.0
    while not _stop.is_set():
        try:
            did = run_one(worker_id)
            if periodic and time.monotonic() - last_periodic > 300:
                last_periodic = time.monotonic()
                for kind in ("retention_sweep",):
                    if kind in HANDLERS:
                        HANDLERS[kind]({})
        except Exception as e:  # noqa: BLE001
            log.error("worker loop error: %s: %s", type(e).__name__, e)
            log.debug("full traceback:\n%s", traceback.format_exc())
            did = False
        if not did:
            _stop.wait(st.worker_poll_s)


def start_workers() -> None:
    if _threads:
        return
    _stop.clear()
    host = socket.gethostname()
    for i in range(get_settings().worker_threads):
        t = threading.Thread(target=_loop, args=(f"{host}-{i}", i == 0), daemon=True, name=f"sereno-worker-{i}")
        t.start()
        _threads.append(t)


def stop_workers() -> None:
    _stop.set()
    for t in _threads:
        t.join(timeout=5)
    _threads.clear()


def reset_stale(minutes: int = 30) -> int:
    """Requeue jobs whose worker died mid-run."""
    with session_scope() as s:
        res = s.execute(text("UPDATE jobs SET status='queued', locked_by=NULL WHERE status='running' AND locked_at < :t"),
                        {"t": utcnow() - timedelta(minutes=minutes)})
        return res.rowcount or 0
