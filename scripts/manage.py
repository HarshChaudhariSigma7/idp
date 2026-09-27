"""Operations CLI.

  python scripts/manage.py create-tenant "Sahyadri Auto Components Ltd" admin@sahyadri.in "Priya Kulkarni"
  python scripts/manage.py create-staff reviewer@serenovolante.com "Rahul" sereno_reviewer
  python scripts/manage.py seed-demo <tenant_id>        # synthetic demo docs, offline fake model
  python scripts/manage.py make-synth eval_data/synthetic
  python scripts/manage.py verify-audit
  python scripts/manage.py retention-sweep
"""
from __future__ import annotations

import random
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sereno.db import init_db, session_scope  # noqa: E402
from sereno.models import Tenant, User  # noqa: E402
from sereno.security.auth import hash_password  # noqa: E402


def _temp_password() -> str:
    return secrets.token_urlsafe(12)


def create_tenant(name: str, admin_email: str, admin_name: str, managed_review: str = "yes"):
    init_db()
    pw = _temp_password()
    with session_scope() as s:
        t = Tenant(name=name, managed_review=managed_review == "yes")
        s.add(t)
        s.flush()
        s.add(User(tenant_id=t.id, email=admin_email.lower(), name=admin_name, role="admin", password_hash=hash_password(pw)))
        print(f"tenant_id={t.id}\nadmin={admin_email}\ntemporary_password={pw}\n(two-step setup is forced at first sign-in)")


def create_user(tenant_id: str, email: str, name: str, role: str):
    init_db()
    pw = _temp_password()
    with session_scope() as s:
        s.add(User(tenant_id=None if tenant_id == "-" else tenant_id, email=email.lower(), name=name, role=role,
                   password_hash=hash_password(pw)))
    print(f"{email} ({role}) temporary_password={pw}")


def create_staff(email: str, name: str, role: str):
    assert role in ("sereno_reviewer", "sereno_ops")
    create_user("-", email, name, role)


def seed_demo(tenant_id: str, n: str = "12"):
    """Synthetic documents through the real pipeline with the offline fake model, including a few
    realistic misreads so the review screen has something to show. Clearly synthetic: do not use
    for accuracy claims."""
    from sereno import jobs, pipeline
    from sereno.eval.fake_model import TruthResponder
    from sereno.eval.synth import make_invoice, make_lr, to_jpeg_bytes
    from sereno.extraction.llm import FakeLLM, set_llm
    init_db()
    n = int(n)
    rng = random.Random(42)
    with session_scope() as s:
        uploader = s.query(User).filter(User.tenant_id == tenant_id).first()
        uid = uploader.id
    plans = ["digital", "good_scan", "good_scan", "poor_scan", "bilingual", "lr"] * (n // 6 + 1)
    for i, kind in enumerate(plans[:n]):
        sd = make_lr(rng) if kind == "lr" else make_invoice(rng, kind, n_lines=rng.randint(2, 5))
        errors = {}
        if i % 3 == 1 and sd.lines:
            errors = {"primary": {"line_items[0].quantity": sd.lines[0]["quantity"] + 1}}
        elif i % 3 == 2 and sd.doc_type == "invoice":
            errors = {"primary": {"grand_total": sd.truth["grand_total"] + 100}, "secondary": {"grand_total": sd.truth["grand_total"] + 100}}
        set_llm(FakeLLM(TruthResponder(sd, errors=errors)))
        data = sd.pdf if sd.pdf else to_jpeg_bytes(sd.image)
        with session_scope() as s:
            pipeline.ingest_upload(s, tenant_id, uid, f"demo_{kind}_{i:02d}.{'pdf' if sd.pdf else 'jpg'}", data, "auto")
        jobs.drain()
    print(f"seeded {n} synthetic documents")


def make_synth(out: str, n: str = "4"):
    from sereno.eval.synth import write_dataset
    paths = write_dataset(Path(out), int(n))
    print(f"wrote {len(paths)} synthetic documents to {out}")


def verify_audit():
    from sereno.security.audit import verify_chain
    init_db()
    ok, bad = verify_chain()
    print("audit chain OK" if ok else f"AUDIT CHAIN BROKEN at seq {bad}")
    sys.exit(0 if ok else 2)


def retention_sweep():
    from sereno.pipeline import retention_sweep as sweep
    init_db()
    print(f"purged {sweep({})} documents")


COMMANDS = {"create-tenant": create_tenant, "create-user": create_user, "create-staff": create_staff,
            "seed-demo": seed_demo, "make-synth": make_synth, "verify-audit": verify_audit,
            "retention-sweep": retention_sweep}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](*sys.argv[2:])
