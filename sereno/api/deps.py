from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from sereno.db import new_session
from sereno.models import AuthSession, User
from sereno.security.auth import load_session
from sereno.security.rbac import can

COOKIE = "sv_session"


def get_db() -> Iterator[Session]:
    s = new_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _session(request: Request, s: Session) -> tuple[AuthSession, User] | None:
    sess = load_session(s, request.cookies.get(COOKIE))
    if sess is None:
        return None
    user = s.get(User, sess.user_id)
    if user is None or not user.is_active:
        return None
    return sess, user


def pre_mfa_user(request: Request, s: Session = Depends(get_db)) -> tuple[AuthSession, User]:
    r = _session(request, s)
    if r is None:
        raise HTTPException(401, "Please sign in")
    return r


def current_user(request: Request, s: Session = Depends(get_db)) -> User:
    r = _session(request, s)
    if r is None:
        raise HTTPException(401, "Please sign in")
    sess, user = r
    if not sess.mfa_passed:
        raise HTTPException(401, "Two-factor verification required")
    return user


def require(perm: str):
    def dep(user: User = Depends(current_user)) -> User:
        if not can(user.role, perm):
            raise HTTPException(403, "You don't have access to this")
        return user
    return dep
