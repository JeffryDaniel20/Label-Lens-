"""FastAPI dependencies for authentication, tenancy and authorization."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.audit.models import ActorType, AuditAction
from app.db.session import get_session_factory, set_tenant_context
from app.identity.models import ApiKey, Membership, MembershipStatus, Organization, Role, User
from app.identity.rbac import Capability, capabilities_for, has_capability
from app.identity.service import authenticate_api_key
from app.identity.sessions import SessionData, SessionManager
from app.platform.config import Settings, get_settings
from app.platform.errors import Forbidden, Unauthenticated
from app.platform.logging import actor_id_var, org_id_var

CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(slots=True)
class Principal:
    """The authenticated caller, always bound to exactly one organization."""

    org_id: uuid.UUID
    role: Role
    user: User | None = None
    api_key: ApiKey | None = None
    session_id: str | None = None
    session: SessionData | None = None

    @property
    def actor_type(self) -> ActorType:
        return ActorType.API_KEY if self.api_key is not None else ActorType.USER

    @property
    def actor_id(self) -> uuid.UUID | None:
        if self.user is not None:
            return self.user.id
        return self.api_key.id if self.api_key else None

    @property
    def actor_label(self) -> str | None:
        if self.user is not None:
            return self.user.email
        return self.api_key.prefix if self.api_key else None

    @property
    def capabilities(self) -> list[str]:
        return capabilities_for(self.role)


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session_manager(request: Request) -> SessionManager:
    manager: SessionManager = request.app.state.session_manager
    return manager


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _load_membership(db: Session, user_id: uuid.UUID, org_id: uuid.UUID) -> Membership:
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == user_id,
            Membership.organization_id == org_id,
            Membership.status == MembershipStatus.ACTIVE,
        )
    )
    if membership is None:
        # Deliberately not 403: an outsider learns nothing about the org.
        raise Unauthenticated("Your session is no longer valid for this organization.")
    return membership


def get_principal(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    sessions: SessionManager = Depends(get_session_manager),
) -> Principal:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        api_key = authenticate_api_key(db, authorization[7:].strip())
        principal = Principal(org_id=api_key.organization_id, role=api_key.role, api_key=api_key)
        _bind_context(db, principal)
        return principal

    session_id = request.cookies.get(settings.session_cookie_name)
    if not session_id:
        raise Unauthenticated("Authentication required.")
    data = sessions.load(session_id)
    if data is None:
        raise Unauthenticated("Your session has expired. Please sign in again.")

    if request.method not in SAFE_METHODS:
        presented = request.headers.get(CSRF_HEADER)
        if not presented or presented != data.csrf_token:
            raise Forbidden("Missing or invalid CSRF token.")

    user = db.get(User, uuid.UUID(data.user_id))
    if user is None or user.status.value != "active":
        sessions.revoke(session_id)
        raise Unauthenticated("Your session is no longer valid.")
    if data.org_id is None:
        raise Forbidden("No organization is selected for this session.")

    membership = _load_membership(db, user.id, uuid.UUID(data.org_id))
    sessions.touch(session_id, data)
    principal = Principal(
        org_id=membership.organization_id,
        role=membership.role,
        user=user,
        session_id=session_id,
        session=data,
    )
    _bind_context(db, principal)
    return principal


def _bind_context(db: Session, principal: Principal) -> None:
    """Bind tenant for RLS and logging context for the remainder of the request."""
    set_tenant_context(db, principal.org_id)
    org_id_var.set(str(principal.org_id))
    if principal.actor_id:
        actor_id_var.set(str(principal.actor_id))


def get_organization(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> Organization:
    org = db.get(Organization, principal.org_id)
    if org is None or org.deleted_at is not None:
        raise Unauthenticated("Organization is unavailable.")
    return org


def require(capability: Capability) -> Callable[..., Principal]:
    """Endpoint dependency enforcing one capability from the RBAC matrix."""

    def _dependency(
        request: Request,
        principal: Principal = Depends(get_principal),
        db: Session = Depends(get_db),
    ) -> Principal:
        if not has_capability(principal.role, capability):
            audit.record_out_of_band(
                action=AuditAction.ACCESS_DENIED,
                actor_type=principal.actor_type,
                actor_id=principal.actor_id,
                actor_label=principal.actor_label,
                organization_id=principal.org_id,
                resource_type="capability",
                resource_id=capability.value,
                ip=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
            raise Forbidden(f"This action requires the '{capability.value}' capability.")
        return principal

    return _dependency
