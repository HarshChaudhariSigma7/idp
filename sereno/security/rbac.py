"""Role-based access control. Permissions are explicit; nothing is implied by a role name."""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Query

ROLES = {
    # Customer roles
    "admin": "Company admin: users, templates, exports, all company documents",
    "manager": "Finance/ops head: dashboard, all company documents, exports, can review",
    "reviewer": "Reviewer: only documents assigned to them",
    "uploader": "Uploader: upload and track own uploads",
    # Sereno staff (tenant_id is NULL)
    "sereno_reviewer": "Sereno managed-review staff: only documents assigned to them, any tenant",
    "sereno_ops": "Sereno internal: aggregate metrics only, no document content",
}

PERMS = {
    "document.upload": {"admin", "manager", "uploader"},
    "document.view_all": {"admin", "manager"},
    "document.view_assigned": {"reviewer", "sereno_reviewer", "admin", "manager"},
    "document.view_own_uploads": {"uploader"},
    "review.work": {"reviewer", "sereno_reviewer", "admin", "manager"},
    "review.assign": {"admin", "manager"},
    "dashboard.view": {"admin", "manager"},
    "export.run": {"admin", "manager"},
    "match.run": {"admin", "manager"},
    "template.manage": {"admin", "manager"},
    "user.manage": {"admin"},
    "audit.view": {"admin"},
    "metrics.internal": {"sereno_ops"},
}


def can(role: str, perm: str) -> bool:
    return role in PERMS.get(perm, set())


def scope_documents(q: Query, user) -> Query:
    """Restrict a Document query to what `user` may see. Reviewers see only assigned documents."""
    from sereno.models import Document
    if user.tenant_id is None:  # Sereno staff
        if user.role == "sereno_reviewer":
            return q.filter(Document.assigned_to == user.id)
        return q.filter(False)  # sereno_ops never sees document content
    q = q.filter(Document.tenant_id == user.tenant_id)
    if can(user.role, "document.view_all"):
        return q
    if user.role == "reviewer":
        return q.filter(Document.assigned_to == user.id)
    if user.role == "uploader":
        return q.filter(Document.uploaded_by == user.id)
    return q.filter(False)


def can_access_document(user, doc) -> bool:
    if user.tenant_id is None:
        return user.role == "sereno_reviewer" and doc.assigned_to == user.id
    if doc.tenant_id != user.tenant_id:
        return False
    if can(user.role, "document.view_all"):
        return True
    if user.role == "reviewer":
        return doc.assigned_to == user.id
    if user.role == "uploader":
        return doc.uploaded_by == user.id
    return False


def can_review_document(user, doc) -> bool:
    if not can(user.role, "review.work"):
        return False
    if user.role in ("reviewer", "sereno_reviewer"):
        return doc.assigned_to == user.id and (user.tenant_id in (None, doc.tenant_id))
    return can_access_document(user, doc)


__all__ = ["ROLES", "PERMS", "can", "scope_documents", "can_access_document", "can_review_document", "or_"]
