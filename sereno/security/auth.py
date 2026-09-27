"""Passwords (argon2id), TOTP MFA (mandatory for every account with document access),
server-side revocable sessions, and lockout after repeated failures."""
from __future__ import annotations

import hashlib
import io
import secrets
from datetime import timedelta

import pyotp
import qrcode
import qrcode.image.svg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from sereno.config import get_settings
from sereno.models import AuthSession, User, utcnow

_ph = PasswordHasher()
ISSUER = "Sereno Volante"


def hash_password(pw: str) -> str:
    if len(pw) < 12:
        raise ValueError("Password must be at least 12 characters")
    return _ph.hash(pw)


def verify_password(user: User, pw: str) -> bool:
    try:
        return _ph.verify(user.password_hash, pw)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthError(Exception):
    pass


def authenticate(s: Session, email: str, password: str) -> User:
    st = get_settings()
    user = s.execute(select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
    if user is None or not user.is_active:
        _ph.hash("timing-equaliser-dummy")
        raise AuthError("Email or password is incorrect")
    if user.locked_until and user.locked_until > utcnow():
        raise AuthError("Account temporarily locked after repeated failed attempts. Try again later.")
    if not verify_password(user, password):
        user.failed_logins += 1
        if user.failed_logins >= st.max_failed_logins:
            user.locked_until = utcnow() + timedelta(minutes=st.lockout_minutes)
            user.failed_logins = 0
        s.flush()
        raise AuthError("Email or password is incorrect")
    user.failed_logins = 0
    return user


def create_session(s: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    s.add(AuthSession(user_id=user.id, token_hash=_token_hash(token), mfa_passed=False,
                      expires_at=utcnow() + timedelta(hours=get_settings().session_hours)))
    s.flush()
    return token


def load_session(s: Session, token: str | None) -> AuthSession | None:
    if not token:
        return None
    sess = s.execute(select(AuthSession).where(AuthSession.token_hash == _token_hash(token))).scalar_one_or_none()
    if sess is None or sess.revoked or sess.expires_at < utcnow():
        return None
    return sess


def revoke_session(s: Session, token: str) -> None:
    sess = load_session(s, token)
    if sess:
        sess.revoked = True


def begin_mfa_enrollment(user: User) -> tuple[str, str]:
    secret = pyotp.random_base32()
    user.mfa_secret = secret
    user.mfa_enabled = False
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=ISSUER)
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return uri, buf.getvalue().decode()


def verify_totp(user: User, code: str) -> bool:
    if not user.mfa_secret:
        return False
    return pyotp.TOTP(user.mfa_secret).verify(code.strip().replace(" ", ""), valid_window=1)
