"""Imports every mapped model so Alembic autogenerate sees the full metadata."""

from __future__ import annotations

from app.analysis.models import Analysis, AnalysisEvent
from app.audit.models import AuditLog
from app.catalog.models import File, FilePage, Product, ProductVersion
from app.db.base import Base
from app.identity.models import ApiKey, Membership, Organization, RecoveryCode, User
from app.rules.models import RuleRow, Ruleset
from app.vision.models import OcrResult, OcrTokenRow

# Tables that carry a tenant column and therefore require RLS + application scoping.
# `rulesets`/`rules` are deliberately absent: regulatory content is shared
# across every organization, not tenant-owned (see app.rules.publish).
TENANT_TABLES: tuple[str, ...] = (
    "memberships",
    "api_keys",
    "products",
    "product_versions",
    "files",
    "file_pages",
    "ocr_results",
    "ocr_tokens",
    "analyses",
    "analysis_events",
    "audit_logs",
)

__all__ = [
    "Base",
    "TENANT_TABLES",
    "Analysis",
    "AnalysisEvent",
    "ApiKey",
    "AuditLog",
    "File",
    "FilePage",
    "Membership",
    "OcrResult",
    "OcrTokenRow",
    "Organization",
    "Product",
    "ProductVersion",
    "RecoveryCode",
    "RuleRow",
    "Ruleset",
    "User",
]
