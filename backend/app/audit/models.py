"""Append-only audit log.

Rows are never updated or deleted. On PostgreSQL a trigger enforces this at the
database level (see the migration); the application never issues such statements.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKey, enum_column, utcnow


class ActorType(enum.StrEnum):
    USER = "user"
    API_KEY = "api_key"
    SYSTEM = "system"
    ANONYMOUS = "anonymous"


class AuditAction(enum.StrEnum):
    LOGIN_SUCCEEDED = "auth.login.succeeded"
    LOGIN_FAILED = "auth.login.failed"
    LOGIN_LOCKED = "auth.login.locked"
    MFA_CHALLENGED = "auth.mfa.challenged"
    MFA_ENROLLED = "auth.mfa.enrolled"
    MFA_FAILED = "auth.mfa.failed"
    LOGOUT = "auth.logout"
    ORG_CREATED = "org.created"
    MEMBER_INVITED = "member.invited"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"
    APIKEY_CREATED = "apikey.created"
    APIKEY_REVOKED = "apikey.revoked"
    ACCESS_DENIED = "access.denied"
    PRODUCT_CREATED = "product.created"
    PRODUCT_UPDATED = "product.updated"
    PRODUCT_VERSION_CREATED = "product_version.created"
    PRODUCT_VERSION_LOCKED = "product_version.locked"
    FILE_UPLOADED = "file.uploaded"
    FILE_REJECTED = "file.rejected"


class AuditLog(UUIDPrimaryKey, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_org_created", "organization_id", "created_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        Index("ix_audit_logs_action", "action"),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="SET NULL"), default=None
    )
    actor_type: Mapped[ActorType] = mapped_column(enum_column(ActorType), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    actor_label: Mapped[str | None] = mapped_column(String(320), default=None)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(60), default=None)
    resource_id: Mapped[str | None] = mapped_column(String(80), default=None)
    before: Mapped[dict[str, object] | None] = mapped_column(JSON, default=None)
    after: Mapped[dict[str, object] | None] = mapped_column(JSON, default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
