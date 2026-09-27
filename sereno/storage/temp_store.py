"""Short-lived encrypted blob storage for raw documents and page images.

Separate from Postgres (system of record). Every object has an expiry; purge_expired() runs on a
schedule and removes the ciphertext plus its wrapped key. Local filesystem backend for pilots;
an S3 backend (SSE-KMS + lifecycle rule as a second safety net) implements the same interface.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sereno.config import get_settings
from sereno.models import utcnow
from sereno.security.crypto import decrypt_blob, encrypt_blob


class BlobGone(FileNotFoundError):
    """The object expired and was deleted under the retention policy."""


class LocalTempStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, namespace: str, doc_id: str) -> Path:
        return self.root / namespace / doc_id

    def put(self, namespace: str, doc_id: str, name: str, data: bytes, expires_at: datetime) -> str:
        d = self._dir(namespace, doc_id)
        d.mkdir(parents=True, exist_ok=True)
        aad = f"{namespace}/{doc_id}/{name}".encode()
        (d / f"{name}.enc").write_bytes(encrypt_blob(data, aad))
        self._set_expiry(d, expires_at)
        return f"{namespace}/{doc_id}/{name}"

    def get(self, namespace: str, doc_id: str, name: str) -> bytes:
        d = self._dir(namespace, doc_id)
        p = d / f"{name}.enc"
        if not p.exists() or self._expired(d):
            raise BlobGone(f"{namespace}/{doc_id}/{name}")
        return decrypt_blob(p.read_bytes(), f"{namespace}/{doc_id}/{name}".encode())

    def exists(self, namespace: str, doc_id: str, name: str) -> bool:
        d = self._dir(namespace, doc_id)
        return (d / f"{name}.enc").exists() and not self._expired(d)

    def set_expiry(self, namespace: str, doc_id: str, expires_at: datetime) -> None:
        d = self._dir(namespace, doc_id)
        if d.exists():
            self._set_expiry(d, expires_at)

    def delete(self, namespace: str, doc_id: str) -> bool:
        d = self._dir(namespace, doc_id)
        if d.exists():
            shutil.rmtree(d)
            return True
        return False

    def purge_expired(self, now: datetime | None = None) -> list[tuple[str, str]]:
        now = now or utcnow()
        purged = []
        for ns in self.root.iterdir() if self.root.exists() else []:
            if not ns.is_dir():
                continue
            for d in ns.iterdir():
                if d.is_dir() and self._expired(d, now):
                    shutil.rmtree(d)
                    purged.append((ns.name, d.name))
        return purged

    @staticmethod
    def _set_expiry(d: Path, expires_at: datetime) -> None:
        (d / "_meta.json").write_text(json.dumps({"expires_at": expires_at.isoformat()}))

    @staticmethod
    def _expired(d: Path, now: datetime | None = None) -> bool:
        meta = d / "_meta.json"
        if not meta.exists():
            return True
        exp = datetime.fromisoformat(json.loads(meta.read_text())["expires_at"])
        return (now or utcnow()) >= exp


_store: LocalTempStore | None = None


def get_store() -> LocalTempStore:
    global _store
    if _store is None:
        _store = LocalTempStore(get_settings().data_dir / "blobs")
    return _store


def reset_store() -> None:
    global _store
    _store = None


def default_expiry() -> datetime:
    return utcnow() + timedelta(hours=get_settings().doc_ttl_hours)


DOCS = "docs"      # raw uploads + page images, time-boxed
EVAL = "eval"      # opt-in evaluation retention only
