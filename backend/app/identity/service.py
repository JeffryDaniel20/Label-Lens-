"""Identity domain services: signup, authentication, membership and API keys."""

from __future__ import annotations

import datetime as dt
import re
import secrets
import uuid
from dataclasses import dataclass

import pyotp
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.audit.models import ActorType, AuditAction
from app.db.base import utcnow
from app.identity.models import (
    ApiKey,
    Membership,
    MembershipStatus,
    Organization,
    RecoveryCode,
    Role,
    User,
    UserStatus,
)
from app.identity.passwords import (
    generate_token,
    hash_password,
    hash_token,
    verify_password,
    verify_token,
)
from app.platform.errors import (
    Conflict,
    Forbidden,
    NotFound,
    RateLimited,
    Unauthenticated,
    ValidationFailed,
)
from app.platform.ratelimit import RateLimiter

API_KEY_PREFIX = "llk"
RECOVERY_CODE_COUNT = 8


class InvalidCredentials(Unauthenticated):
    error_type = "invalid_credentials"
    title = "Invalid email or password"


class MfaRequired(Unauthenticated):
    error_type = "mfa_required"
    title = "Multi-factor authentication code required"


class AccountLocked(Unauthenticated):
    status_code = 429
    error_type = "account_locked"
    title = "Too many failed attempts"


@dataclass(slots=True)
class AuthenticatedUser:
    user: User
    membership: Membership | None


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:60] or "org"


# --------------------------------------------------------------------------
# organizations & users
# --------------------------------------------------------------------------


def create_organization_with_owner(
    db: Session,
    *,
    organization_name: str,
    email: str,
    password: str,
    display_name: str = "",
    ip: str | None = None,
    limiter: RateLimiter | None = None,
    max_attempts_per_ip: int = 5,
    window_seconds: int = 60 * 60,
) -> tuple[Organization, User]:
    # Unauthenticated and genuinely expensive (Argon2 hashing plus an
    # org+user+membership insert) - the one mutating endpoint with no
    # per-account identity to key a lockout on, so this limits by IP
    # instead. `limiter` is optional only so direct, non-HTTP callers
    # (scripts, future admin tooling) aren't forced to wire one up; the real
    # `/v1/auth/signup` endpoint always passes one.
    if limiter is not None:
        allowed, retry_after = limiter.hit(
            f"signup:{ip or 'noip'}", limit=max_attempts_per_ip, window_seconds=window_seconds
        )
        if not allowed:
            raise RateLimited(
                "Too many accounts created from this address recently. "
                f"Try again in {retry_after or window_seconds} seconds.",
                retry_after=retry_after or window_seconds,
            )

    email = email.strip().lower()
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise Conflict("An account with that email already exists.")

    slug_base = slugify(organization_name)
    slug = slug_base
    while db.scalar(select(Organization).where(Organization.slug == slug)) is not None:
        slug = f"{slug_base}-{secrets.token_hex(3)}"

    org = Organization(name=organization_name, slug=slug)
    user = User(email=email, display_name=display_name, password_hash=hash_password(password))
    db.add_all([org, user])
    db.flush()
    db.add(
        Membership(
            organization_id=org.id,
            user_id=user.id,
            role=Role.OWNER,
            status=MembershipStatus.ACTIVE,
        )
    )
    db.flush()
    audit.record(
        db,
        action=AuditAction.ORG_CREATED,
        actor_type=ActorType.USER,
        actor_id=user.id,
        actor_label=user.email,
        organization_id=org.id,
        resource_type="organization",
        resource_id=org.id,
        after={"name": org.name, "slug": org.slug},
        ip=ip,
    )
    return org, user


def update_organization(
    db: Session,
    *,
    org: Organization,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
    changes: dict[str, object],
    ip: str | None = None,
) -> Organization:
    """Admin's own "manage... retention settings" capability, finally
    wired: `retention_days`/`cloud_ai_enabled` existed and were already
    read back via `OrganizationOut`, but nothing let an Admin change either
    one - a production-readiness audit finding, closed here, mirroring
    `app.catalog.service.update_product`'s own shape exactly."""
    before = {k: getattr(org, k) for k in changes}
    for key, value in changes.items():
        setattr(org, key, value)
    db.flush()
    audit.record(
        db,
        action=AuditAction.ORG_UPDATED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=org.id,
        resource_type="organization",
        resource_id=org.id,
        before=before,
        after=changes,
        ip=ip,
    )
    return org


def add_member(
    db: Session,
    *,
    org: Organization,
    actor: User,
    email: str,
    password: str,
    role: Role,
    display_name: str = "",
    ip: str | None = None,
) -> Membership:
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, display_name=display_name, password_hash=hash_password(password))
        db.add(user)
        db.flush()
    existing = db.scalar(
        select(Membership).where(
            Membership.organization_id == org.id, Membership.user_id == user.id
        )
    )
    if existing is not None:
        raise Conflict("That user is already a member of this organization.")
    membership = Membership(
        organization_id=org.id, user_id=user.id, role=role, status=MembershipStatus.ACTIVE
    )
    db.add(membership)
    db.flush()
    audit.record(
        db,
        action=AuditAction.MEMBER_INVITED,
        actor_type=ActorType.USER,
        actor_id=actor.id,
        actor_label=actor.email,
        organization_id=org.id,
        resource_type="membership",
        resource_id=membership.id,
        after={"email": email, "role": role.value},
        ip=ip,
    )
    return membership


def change_member_role(
    db: Session,
    *,
    org: Organization,
    actor: User,
    membership: Membership,
    new_role: Role,
    ip: str | None = None,
) -> Membership:
    if membership.organization_id != org.id:
        raise NotFound("Membership not found.")
    if membership.role is Role.OWNER and new_role is not Role.OWNER:
        owners = db.scalars(
            select(Membership).where(
                Membership.organization_id == org.id,
                Membership.role == Role.OWNER,
                Membership.status == MembershipStatus.ACTIVE,
            )
        ).all()
        if len(owners) <= 1:
            raise Conflict("An organization must retain at least one Owner.")
    before = {"role": membership.role.value}
    membership.role = new_role
    db.flush()
    audit.record(
        db,
        action=AuditAction.MEMBER_ROLE_CHANGED,
        actor_type=ActorType.USER,
        actor_id=actor.id,
        actor_label=actor.email,
        organization_id=org.id,
        resource_type="membership",
        resource_id=membership.id,
        before=before,
        after={"role": new_role.value},
        ip=ip,
    )
    return membership


# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------


def _lockout_key(email: str, ip: str | None) -> str:
    return f"login:{hash_token(email)}:{ip or 'noip'}"


def authenticate(
    db: Session,
    *,
    email: str,
    password: str,
    limiter: RateLimiter,
    max_attempts: int,
    lockout_seconds: int,
    totp_code: str | None = None,
    recovery_code: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuthenticatedUser:
    email = email.strip().lower()
    key = _lockout_key(email, ip)
    locked, retry_after = limiter.is_locked(key, limit=max_attempts)
    if locked:
        audit.record_out_of_band(
            action=AuditAction.LOGIN_LOCKED,
            actor_type=ActorType.ANONYMOUS,
            actor_label=email,
            ip=ip,
            user_agent=user_agent,
        )
        raise AccountLocked(
            f"Too many failed attempts. Try again in {retry_after or lockout_seconds} seconds."
        )

    user = db.scalar(select(User).where(User.email == email))
    password_ok = verify_password(password, user.password_hash if user else None)

    if user is None or not password_ok or user.status is not UserStatus.ACTIVE:
        limiter.hit(key, limit=max_attempts, window_seconds=lockout_seconds)
        audit.record_out_of_band(
            action=AuditAction.LOGIN_FAILED,
            actor_type=ActorType.ANONYMOUS,
            actor_label=email,
            ip=ip,
            user_agent=user_agent,
        )
        # Identical response whether the user exists, is disabled, or the
        # password is wrong - no account enumeration.
        raise InvalidCredentials("Invalid email or password.")

    if user.mfa_enabled:
        if not _verify_second_factor(db, user, totp_code=totp_code, recovery_code=recovery_code):
            limiter.hit(key, limit=max_attempts, window_seconds=lockout_seconds)
            audit.record_out_of_band(
                action=AuditAction.MFA_FAILED
                if totp_code or recovery_code
                else AuditAction.MFA_CHALLENGED,
                actor_type=ActorType.USER,
                actor_id=user.id,
                actor_label=user.email,
                ip=ip,
                user_agent=user_agent,
            )
            raise MfaRequired("A valid authentication code is required.")

    limiter.clear(key)
    user.last_login_at = utcnow()
    membership = db.scalar(
        select(Membership)
        .where(Membership.user_id == user.id, Membership.status == MembershipStatus.ACTIVE)
        .order_by(Membership.created_at.asc())
    )
    audit.record(
        db,
        action=AuditAction.LOGIN_SUCCEEDED,
        actor_type=ActorType.USER,
        actor_id=user.id,
        actor_label=user.email,
        organization_id=membership.organization_id if membership else None,
        ip=ip,
        user_agent=user_agent,
    )
    return AuthenticatedUser(user=user, membership=membership)


def _verify_second_factor(
    db: Session, user: User, *, totp_code: str | None, recovery_code: str | None
) -> bool:
    if totp_code and user.mfa_secret:
        if pyotp.TOTP(user.mfa_secret).verify(totp_code, valid_window=1):
            return True
    if recovery_code:
        codes = db.scalars(
            select(RecoveryCode).where(
                RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
            )
        ).all()
        for candidate in codes:
            if verify_token(recovery_code.strip(), candidate.code_hash):
                candidate.used_at = utcnow()
                db.flush()
                return True
    return False


# --------------------------------------------------------------------------
# MFA enrolment
# --------------------------------------------------------------------------


def start_mfa_enrolment(user: User) -> tuple[str, str]:
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="LabelLens")
    return secret, uri


def confirm_mfa_enrolment(
    db: Session, *, user: User, secret: str, totp_code: str, ip: str | None = None
) -> list[str]:
    if not pyotp.TOTP(secret).verify(totp_code, valid_window=1):
        raise ValidationFailed("That authentication code is not valid.")
    user.mfa_secret = secret
    user.mfa_enabled = True
    for existing in list(user.recovery_codes):
        db.delete(existing)
    plaintext: list[str] = []
    for _ in range(RECOVERY_CODE_COUNT):
        code = generate_token(8)
        plaintext.append(code)
        db.add(RecoveryCode(user_id=user.id, code_hash=hash_token(code)))
    db.flush()
    audit.record(
        db,
        action=AuditAction.MFA_ENROLLED,
        actor_type=ActorType.USER,
        actor_id=user.id,
        actor_label=user.email,
        resource_type="user",
        resource_id=user.id,
        ip=ip,
    )
    return plaintext


def require_mfa_for_role(role: Role) -> bool:
    return role in (Role.OWNER, Role.ADMIN)


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------


def create_api_key(
    db: Session, *, org: Organization, actor: User, name: str, role: Role, ip: str | None = None
) -> tuple[str, ApiKey]:
    if role in (Role.OWNER, Role.ADMIN):
        raise Forbidden("API keys may not be granted Owner or Admin roles.")
    secret = generate_token(32)
    prefix = f"{API_KEY_PREFIX}_{secrets.token_hex(4)}"
    plaintext = f"{prefix}.{secret}"
    api_key = ApiKey(
        organization_id=org.id,
        created_by_user_id=actor.id,
        name=name,
        prefix=prefix,
        key_hash=hash_token(secret),
        role=role,
    )
    db.add(api_key)
    db.flush()
    audit.record(
        db,
        action=AuditAction.APIKEY_CREATED,
        actor_type=ActorType.USER,
        actor_id=actor.id,
        actor_label=actor.email,
        organization_id=org.id,
        resource_type="api_key",
        resource_id=api_key.id,
        after={"name": name, "role": role.value, "prefix": prefix},
        ip=ip,
    )
    return plaintext, api_key


def revoke_api_key(
    db: Session, *, org: Organization, actor: User, key_id: uuid.UUID, ip: str | None = None
) -> ApiKey:
    api_key = db.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.organization_id == org.id)
    )
    if api_key is None:
        raise NotFound("API key not found.")
    if api_key.revoked_at is None:
        api_key.revoked_at = utcnow()
        db.flush()
        audit.record(
            db,
            action=AuditAction.APIKEY_REVOKED,
            actor_type=ActorType.USER,
            actor_id=actor.id,
            actor_label=actor.email,
            organization_id=org.id,
            resource_type="api_key",
            resource_id=api_key.id,
            ip=ip,
        )
    return api_key


def authenticate_api_key(db: Session, presented: str) -> ApiKey:
    if "." not in presented:
        raise Unauthenticated("Malformed API key.")
    prefix, _, secret = presented.partition(".")
    api_key = db.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if api_key is None or not verify_token(secret, api_key.key_hash) or not api_key.is_active:
        raise Unauthenticated("Invalid or revoked API key.")
    api_key.last_used_at = utcnow()
    db.flush()
    return api_key


def stale_before(days: int) -> dt.datetime:
    return utcnow() - dt.timedelta(days=days)
