"""Request and response models for the identity API."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.identity.models import MembershipStatus, Role, UserStatus


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    totp_code: str | None = Field(default=None, pattern=r"^\d{6}$")
    recovery_code: str | None = Field(default=None, max_length=64)


class LoginResponse(BaseModel):
    user_id: uuid.UUID
    org_id: uuid.UUID | None
    csrf_token: str
    mfa_required: bool = False


class MfaChallengeResponse(BaseModel):
    mfa_required: bool = True
    detail: str = "Multi-factor authentication code required."


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    retention_days: int
    cloud_ai_enabled: bool


class MembershipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    organization_id: uuid.UUID
    organization_name: str
    role: Role
    status: MembershipStatus


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    status: UserStatus
    mfa_enabled: bool
    active_org_id: uuid.UUID | None
    role: Role | None
    capabilities: list[str]
    memberships: list[MembershipOut]
    # A cookie-session caller's CSRF token, so a page reload can recover it
    # without re-authenticating - `login`/`signup` are the only other places
    # this value is ever returned, and both happen once per session, not
    # once per page load. `None` for an API-key caller, which has no CSRF
    # token at all (bearer auth isn't subject to CSRF the way a browser
    # cookie is).
    csrf_token: str | None = None


class SignupRequest(BaseModel):
    """Bootstraps a new organization together with its Owner."""

    organization_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(default="", max_length=200)


class MemberCreateRequest(BaseModel):
    email: EmailStr
    role: Role
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(default="", max_length=200)


class MemberRoleUpdateRequest(BaseModel):
    role: Role


class MemberOut(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Role
    status: MembershipStatus


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: Role = Role.ANALYST


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    prefix: str
    role: Role
    created_at: dt.datetime
    last_used_at: dt.datetime | None
    revoked_at: dt.datetime | None


class ApiKeyCreatedOut(BaseModel):
    key: str
    api_key: ApiKeyOut


class MfaEnrolStartResponse(BaseModel):
    secret: str
    otpauth_uri: str


class MfaEnrolConfirmRequest(BaseModel):
    totp_code: str = Field(pattern=r"^\d{6}$")


class MfaEnrolConfirmResponse(BaseModel):
    recovery_codes: list[str]


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    action: str
    actor_type: str
    actor_id: uuid.UUID | None
    actor_label: str | None
    resource_type: str | None
    resource_id: str | None
    ip: str | None
    correlation_id: str | None
    created_at: dt.datetime
