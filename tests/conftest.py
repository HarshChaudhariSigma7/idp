import os
import random

import pytest


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENO_ENV", "test")
    monkeypatch.setenv("SERENO_DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SERENO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SERENO_COOKIE_SECURE", "false")
    monkeypatch.setenv("SERENO_QA_SAMPLE_RATE", "0")
    from sereno import config, db
    from sereno.extraction import llm
    from sereno.security import crypto
    from sereno.storage import temp_store
    config.get_settings.cache_clear()
    db.reset_engine()
    crypto.reset_key_provider()
    temp_store.reset_store()
    llm.set_llm(None)
    db.init_db()
    random.seed(0)
    yield
    db.reset_engine()
    config.get_settings.cache_clear()


@pytest.fixture
def tenant_and_users():
    from sereno.db import session_scope
    from sereno.models import Tenant, User
    from sereno.security.auth import hash_password
    with session_scope() as s:
        t = Tenant(name="Sahyadri Auto Components Ltd", managed_review=False)
        s.add(t)
        s.flush()
        users = {}
        for role in ("admin", "manager", "reviewer", "uploader"):
            u = User(tenant_id=t.id, email=f"{role}@sahyadri.test", name=role.title(), role=role,
                     password_hash=hash_password("correct horse battery"))
            s.add(u)
            s.flush()
            users[role] = u.id
        return t.id, users
