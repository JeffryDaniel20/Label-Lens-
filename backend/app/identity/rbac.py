"""Role-based capability matrix.

The matrix is the single source of truth for what a role may do. Endpoints
declare a capability; they never test for a role directly.
"""

from __future__ import annotations

import enum

from app.identity.models import Role


class Capability(enum.StrEnum):
    # organization administration
    ORG_VIEW = "org:view"
    ORG_UPDATE = "org:update"
    ORG_DELETE = "org:delete"
    ORG_BILLING = "org:billing"
    MEMBER_VIEW = "member:view"
    MEMBER_MANAGE = "member:manage"
    APIKEY_MANAGE = "apikey:manage"
    AUDIT_VIEW = "audit:view"

    # catalog
    PRODUCT_VIEW = "product:view"
    PRODUCT_MANAGE = "product:manage"
    FILE_UPLOAD = "file:upload"

    # analysis + review
    ANALYSIS_VIEW = "analysis:view"
    ANALYSIS_RUN = "analysis:run"
    FINDING_DECIDE = "finding:decide"
    ANALYSIS_SIGNOFF = "analysis:signoff"

    # reporting
    REPORT_VIEW = "report:view"
    REPORT_GENERATE = "report:generate"


_VIEWER: frozenset[Capability] = frozenset(
    {
        Capability.ORG_VIEW,
        Capability.PRODUCT_VIEW,
        Capability.ANALYSIS_VIEW,
        Capability.REPORT_VIEW,
        Capability.REPORT_GENERATE,
    }
)

_ANALYST: frozenset[Capability] = _VIEWER | {
    Capability.PRODUCT_MANAGE,
    Capability.FILE_UPLOAD,
    Capability.ANALYSIS_RUN,
}

_REVIEWER: frozenset[Capability] = _ANALYST | {
    Capability.FINDING_DECIDE,
    Capability.ANALYSIS_SIGNOFF,
}

_ADMIN: frozenset[Capability] = _REVIEWER | {
    Capability.ORG_UPDATE,
    Capability.MEMBER_VIEW,
    Capability.MEMBER_MANAGE,
    Capability.APIKEY_MANAGE,
    Capability.AUDIT_VIEW,
}

_OWNER: frozenset[Capability] = _ADMIN | {
    Capability.ORG_DELETE,
    Capability.ORG_BILLING,
}

CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.VIEWER: _VIEWER,
    Role.ANALYST: _ANALYST,
    Role.REVIEWER: _REVIEWER,
    Role.ADMIN: _ADMIN,
    Role.OWNER: _OWNER,
}


def has_capability(role: Role, capability: Capability) -> bool:
    return capability in CAPABILITIES[role]


def capabilities_for(role: Role) -> list[str]:
    return sorted(c.value for c in CAPABILITIES[role])
