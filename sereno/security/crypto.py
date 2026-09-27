"""AES-256-GCM encryption for everything at rest.

Design (built so customer-managed keys are an additive change, not a rewrite):
  * KeyProvider owns key-encryption keys (KEKs). LocalKeyProvider holds one master key from
    config/secret manager. A KMS-backed provider (AWS KMS / customer CMK) implements the same
    three methods.
  * Blobs (documents, page images) use envelope encryption: a fresh random 256-bit data key per
    object, wrapped by the KEK. Deleting the wrapped key crypto-shreds the object.
  * Database columns holding extracted values/PII use a column key derived from the KEK. Every
    ciphertext carries its key id, so keys can rotate and tenants can later bring their own key.
"""
from __future__ import annotations

import base64
import json
import os
import threading
from typing import Any, Protocol

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.types import Text, TypeDecorator

from sereno.config import get_settings

_PREFIX = "e1"


class KeyProvider(Protocol):
    current_key_id: str

    def wrap(self, dek: bytes) -> tuple[str, bytes]: ...
    def unwrap(self, key_id: str, wrapped: bytes) -> bytes: ...
    def column_key(self, key_id: str) -> bytes: ...
    def hmac_key(self) -> bytes: ...


class LocalKeyProvider:
    def __init__(self, master_key: bytes, key_id: str):
        if len(master_key) != 32:
            raise ValueError("master key must be 32 bytes (AES-256)")
        self._keys = {key_id: master_key}
        self.current_key_id = key_id
        self._col_cache: dict[str, bytes] = {}

    def _derive(self, key_id: str, info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(self._keys[key_id])

    def wrap(self, dek: bytes) -> tuple[str, bytes]:
        kek = self._derive(self.current_key_id, b"sereno-kek")
        nonce = os.urandom(12)
        return self.current_key_id, nonce + AESGCM(kek).encrypt(nonce, dek, b"dek")

    def unwrap(self, key_id: str, wrapped: bytes) -> bytes:
        kek = self._derive(key_id, b"sereno-kek")
        return AESGCM(kek).decrypt(wrapped[:12], wrapped[12:], b"dek")

    def column_key(self, key_id: str) -> bytes:
        if key_id not in self._col_cache:
            self._col_cache[key_id] = self._derive(key_id, b"sereno-db-columns")
        return self._col_cache[key_id]

    def hmac_key(self) -> bytes:
        return self._derive(self.current_key_id, b"sereno-pseudonymise")


_provider: KeyProvider | None = None
_lock = threading.Lock()


def _load_master_key() -> bytes:
    s = get_settings()
    if s.master_key_b64:
        return base64.b64decode(s.master_key_b64)
    if s.env == "prod":
        raise RuntimeError("SERENO_MASTER_KEY_B64 must be set in prod (from KMS/secret manager)")
    path = s.data_dir / ".dev_master_key"
    if path.exists():
        return base64.b64decode(path.read_text().strip())
    key = AESGCM.generate_key(bit_length=256)
    path.write_text(base64.b64encode(key).decode())
    os.chmod(path, 0o600)
    return key


def get_key_provider() -> KeyProvider:
    global _provider
    with _lock:
        if _provider is None:
            _provider = LocalKeyProvider(_load_master_key(), get_settings().master_key_id)
        return _provider


def reset_key_provider() -> None:  # tests
    global _provider
    _provider = None


# --- Envelope encryption for blobs -------------------------------------------------------------

def encrypt_blob(plaintext: bytes, aad: bytes = b"") -> bytes:
    kp = get_key_provider()
    dek = AESGCM.generate_key(bit_length=256)
    key_id, wrapped = kp.wrap(dek)
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext, aad)
    header = json.dumps({"k": key_id, "w": base64.b64encode(wrapped).decode()}).encode()
    return len(header).to_bytes(4, "big") + header + nonce + ct


def decrypt_blob(blob: bytes, aad: bytes = b"") -> bytes:
    kp = get_key_provider()
    hlen = int.from_bytes(blob[:4], "big")
    header = json.loads(blob[4:4 + hlen])
    dek = kp.unwrap(header["k"], base64.b64decode(header["w"]))
    body = blob[4 + hlen:]
    return AESGCM(dek).decrypt(body[:12], body[12:], aad)


# --- Column encryption -------------------------------------------------------------------------

def encrypt_str(value: str) -> str:
    kp = get_key_provider()
    nonce = os.urandom(12)
    ct = AESGCM(kp.column_key(kp.current_key_id)).encrypt(nonce, value.encode(), None)
    return f"{_PREFIX}:{kp.current_key_id}:{base64.b64encode(nonce + ct).decode()}"


def decrypt_str(token: str) -> str:
    prefix, key_id, payload = token.split(":", 2)
    if prefix != _PREFIX:
        raise ValueError("unknown ciphertext format")
    raw = base64.b64decode(payload)
    return AESGCM(get_key_provider().column_key(key_id)).decrypt(raw[:12], raw[12:], None).decode()


def pseudonymise(value: str) -> str:
    """Stable keyed hash for joining on identifiers (e.g. vendor GSTIN) without storing them."""
    h = hmac.HMAC(get_key_provider().hmac_key(), hashes.SHA256())
    h.update(value.strip().upper().encode())
    return h.finalize().hex()[:32]


class EncryptedJSON(TypeDecorator):
    """Transparently AES-GCM encrypts any JSON-serialisable value in a TEXT column."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect):
        if value is None:
            return None
        return encrypt_str(json.dumps(value, ensure_ascii=False, default=str))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(decrypt_str(value))
