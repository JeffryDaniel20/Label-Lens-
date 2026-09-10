"""Identity HTTP endpoints: /v1/auth, /v1/me, /v1/members, /v1/api-keys."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.audit.models import ActorType, AuditAction
from app.identity import schemas, service
from app.identity.deps import (
    Principal,
    get_db,
    get_organization,
    get_principal,
    get_session_manager,
    require,
)
from app.identity.models import Membership, MembershipStatus, Organization, Role, User
from app.identity.rbac import Capability, capabilities_for
from app.identity.sessions import SessionManager
from app.platform.config import Settings, get_settings
from app.platform.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.platform.ratelimit import RateLimiter

router = APIRouter(prefix="/v1", tags=["identity"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


def _set_session_cookie(
    response: Response, session_id: str, settings: Settings
) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        max_age=settings.session_idle_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/auth/signup", response_model=schemas.LoginResponse, status_code=201)
def signup(
    payload: schemas.SignupRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    sessions: SessionManager = Depends(get_session_manager),
) -> schemas.LoginResponse:
    org, user = service.create_organization_with_owner(
        db,
        organization_name=payload.organization_name,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        ip=_client_ip(request),
    )
    session_id, data = sessions.create(user_id=user.id, org_id=org.id, ip=_client_ip(request))
    _set_session_cookie(response, session_id, settings)
    return schemas.LoginResponse(user_id=user.id, org_id=org.id, csrf_token=data.csrf_token)


@router.post("/auth/login", response_model=schemas.LoginResponse)
def login(
    payload: schemas.LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    sessions: SessionManager = Depends(get_session_manager),
) -> schemas.LoginResponse:
    result = service.authenticate(
        db,
        email=payload.email,
        password=payload.password,
        limiter=_limiter(request),
        max_attempts=settings.login_max_attempts,
        lockout_seconds=settings.login_lockout_seconds,
        totp_code=payload.totp_code,
        recovery_code=payload.recovery_code,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    org_id = result.membership.organization_id if result.membership else None
    session_id, data = sessions.create(
        user_id=result.user.id, org_id=org_id, ip=_client_ip(request)
    )
    _set_session_cookie(response, session_id, settings)
    return schemas.LoginResponse(user_id=result.user.id, org_id=org_id, csrf_token=data.csrf_token)


@router.post("/auth/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    sessions: SessionManager = Depends(get_session_manager),
) -> Response:
    if principal.session_id:
        sessions.revoke(principal.session_id)
    audit.record(
        db,
        action=AuditAction.LOGOUT,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        actor_label=principal.actor_label,
        organization_id=principal.org_id,
        ip=_client_ip(request),
    )
    response.delete_cookie(settings.session_cookie_name, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=schemas.MeResponse)
def me(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> schemas.MeResponse:
    if principal.user is None:
        raise Forbidden("This endpoint is only available to user sessions.")
    rows = db.execute(
        select(Membership, Organization)
        .join(Organization, Organization.id == Membership.organization_id)
        .where(Membership.user_id == principal.user.id)
    ).all()
    memberships = [
        schemas.MembershipOut(
            organization_id=m.organization_id,
            organization_name=o.name,
            role=m.role,
            status=m.status,
        )
        for m, o in rows
    ]
    return schemas.MeResponse(
        id=principal.user.id,
        email=principal.user.email,
        display_name=principal.user.display_name,
        status=principal.user.status,
        mfa_enabled=principal.user.mfa_enabled,
        active_org_id=principal.org_id,
        role=principal.role,
        capabilities=capabilities_for(principal.role),
        memberships=memberships,
        csrf_token=principal.session.csrf_token if principal.session else None,
    )


# --------------------------------------------------------------------------
# members
# --------------------------------------------------------------------------


@router.get("/members", response_model=list[schemas.MemberOut])
def list_members(
    principal: Principal = Depends(require(Capability.MEMBER_VIEW)),
    db: Session = Depends(get_db),
) -> list[schemas.MemberOut]:
    rows = db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.organization_id == principal.org_id)
        .order_by(Membership.created_at.asc())
    ).all()
    return [
        schemas.MemberOut(
            membership_id=m.id,
            user_id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=m.role,
            status=m.status,
        )
        for m, u in rows
    ]


@router.post("/members", response_model=schemas.MemberOut, status_code=201)
def create_member(
    payload: schemas.MemberCreateRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.MEMBER_MANAGE)),
    org: Organization = Depends(get_organization),
    db: Session = Depends(get_db),
) -> schemas.MemberOut:
    if principal.user is None:
        raise Forbidden("Member management requires a user session.")
    if payload.role is Role.OWNER and principal.role is not Role.OWNER:
        raise Forbidden("Only an Owner may grant the Owner role.")
    membership = service.add_member(
        db,
        org=org,
        actor=principal.user,
        email=payload.email,
        password=payload.password,
        role=payload.role,
        display_name=payload.display_name,
        ip=_client_ip(request),
    )
    user = db.get(User, membership.user_id)
    assert user is not None
    return schemas.MemberOut(
        membership_id=membership.id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role,
        status=membership.status,
    )


@router.patch("/members/{membership_id}", response_model=schemas.MemberOut)
def update_member_role(
    membership_id: uuid.UUID,
    payload: schemas.MemberRoleUpdateRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.MEMBER_MANAGE)),
    org: Organization = Depends(get_organization),
    db: Session = Depends(get_db),
) -> schemas.MemberOut:
    if principal.user is None:
        raise Forbidden("Member management requires a user session.")
    membership = db.scalar(
        select(Membership).where(
            Membership.id == membership_id, Membership.organization_id == org.id
        )
    )
    if membership is None:
        raise NotFound("Membership not found.")
    if payload.role is Role.OWNER and principal.role is not Role.OWNER:
        raise Forbidden("Only an Owner may grant the Owner role.")
    service.change_member_role(
        db,
        org=org,
        actor=principal.user,
        membership=membership,
        new_role=payload.role,
        ip=_client_ip(request),
    )
    user = db.get(User, membership.user_id)
    assert user is not None
    return schemas.MemberOut(
        membership_id=membership.id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role,
        status=membership.status,
    )


@router.delete("/members/{membership_id}", status_code=204)
def remove_member(
    membership_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.MEMBER_MANAGE)),
    org: Organization = Depends(get_organization),
    db: Session = Depends(get_db),
) -> Response:
    membership = db.scalar(
        select(Membership).where(
            Membership.id == membership_id, Membership.organization_id == org.id
        )
    )
    if membership is None:
        raise NotFound("Membership not found.")
    if membership.role is Role.OWNER:
        owners = db.scalars(
            select(Membership).where(
                Membership.organization_id == org.id,
                Membership.role == Role.OWNER,
                Membership.status == MembershipStatus.ACTIVE,
            )
        ).all()
        if len(owners) <= 1:
            raise Conflict("An organization must retain at least one Owner.")
    membership.status = MembershipStatus.REVOKED
    audit.record(
        db,
        action=AuditAction.MEMBER_REMOVED,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        actor_label=principal.actor_label,
        organization_id=org.id,
        resource_type="membership",
        resource_id=membership.id,
        ip=_client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# MFA
# --------------------------------------------------------------------------


@router.post("/me/mfa/start", response_model=schemas.MfaEnrolStartResponse)
def mfa_start(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> schemas.MfaEnrolStartResponse:
    if principal.user is None:
        raise Forbidden("MFA enrolment requires a user session.")
    secret, uri = service.start_mfa_enrolment(principal.user)
    principal.user.mfa_secret = secret
    principal.user.mfa_enabled = False
    db.flush()
    return schemas.MfaEnrolStartResponse(secret=secret, otpauth_uri=uri)


@router.post("/me/mfa/confirm", response_model=schemas.MfaEnrolConfirmResponse)
def mfa_confirm(
    payload: schemas.MfaEnrolConfirmRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> schemas.MfaEnrolConfirmResponse:
    if principal.user is None:
        raise Forbidden("MFA enrolment requires a user session.")
    if not principal.user.mfa_secret:
        raise ValidationFailed("Start MFA enrolment before confirming it.")
    codes = service.confirm_mfa_enrolment(
        db,
        user=principal.user,
        secret=principal.user.mfa_secret,
        totp_code=payload.totp_code,
        ip=_client_ip(request),
    )
    return schemas.MfaEnrolConfirmResponse(recovery_codes=codes)


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------


@router.get("/api-keys", response_model=list[schemas.ApiKeyOut])
def list_api_keys(
    principal: Principal = Depends(require(Capability.APIKEY_MANAGE)),
    db: Session = Depends(get_db),
) -> list[schemas.ApiKeyOut]:
    from app.identity.models import ApiKey

    keys = db.scalars(
        select(ApiKey)
        .where(ApiKey.organization_id == principal.org_id)
        .order_by(ApiKey.created_at.desc())
    ).all()
    return [schemas.ApiKeyOut.model_validate(k) for k in keys]


@router.post("/api-keys", response_model=schemas.ApiKeyCreatedOut, status_code=201)
def create_api_key(
    payload: schemas.ApiKeyCreateRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.APIKEY_MANAGE)),
    org: Organization = Depends(get_organization),
    db: Session = Depends(get_db),
) -> schemas.ApiKeyCreatedOut:
    if principal.user is None:
        raise Forbidden("API key management requires a user session.")
    plaintext, api_key = service.create_api_key(
        db,
        org=org,
        actor=principal.user,
        name=payload.name,
        role=payload.role,
        ip=_client_ip(request),
    )
    return schemas.ApiKeyCreatedOut(
        key=plaintext, api_key=schemas.ApiKeyOut.model_validate(api_key)
    )


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.APIKEY_MANAGE)),
    org: Organization = Depends(get_organization),
    db: Session = Depends(get_db),
) -> Response:
    if principal.user is None:
        raise Forbidden("API key management requires a user session.")
    service.revoke_api_key(
        db, org=org, actor=principal.user, key_id=key_id, ip=_client_ip(request)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# audit log
# --------------------------------------------------------------------------


@router.get("/audit-logs", response_model=list[schemas.AuditLogOut])
def list_audit_logs(
    action: str | None = None,
    resource_type: str | None = None,
    limit: int = 50,
    principal: Principal = Depends(require(Capability.AUDIT_VIEW)),
    db: Session = Depends(get_db),
) -> list[schemas.AuditLogOut]:
    stmt = audit.query(
        organization_id=principal.org_id, action=action, resource_type=resource_type
    ).limit(min(max(limit, 1), 100))
    return [schemas.AuditLogOut.model_validate(row) for row in db.scalars(stmt).all()]


__all__ = ["router", "ActorType"]
