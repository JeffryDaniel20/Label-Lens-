"""Audit writing helper. Every state change routes through `record()`."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.audit.models import ActorType, AuditAction, AuditLog
from app.db.session import session_scope
from app.platform.logging import correlation_id_var


def record(
    db: Session,
    *,
    action: AuditAction | str,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: uuid.UUID | None = None,
    actor_label: str | None = None,
    organization_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        organization_id=organization_id,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        action=action.value if isinstance(action, AuditAction) else action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        before=before,
        after=after,
        ip=ip,
        user_agent=(user_agent or "")[:300] or None,
        correlation_id=correlation_id_var.get(),
    )
    db.add(entry)
    db.flush()
    return entry


def record_out_of_band(**kwargs: Any) -> None:
    """Write an audit row in its own transaction.

    Denials and failed logins raise, which rolls the request transaction back.
    Those events must still be recorded, so they are written independently.
    """
    with session_scope() as session:
        record(session, **kwargs)


def query(
    *,
    organization_id: uuid.UUID,
    action: str | None = None,
    resource_type: str | None = None,
) -> Select[Any]:
    stmt = select(AuditLog).where(AuditLog.organization_id == organization_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    return stmt.order_by(AuditLog.created_at.desc())
