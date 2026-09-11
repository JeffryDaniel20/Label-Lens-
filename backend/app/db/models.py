"""Imports every mapped model so Alembic autogenerate sees the full metadata."""

from __future__ import annotations

from app.analysis.models import Analysis, AnalysisEvent, DeadLetterJob
from app.audit.models import AuditLog
from app.catalog.models import File, FilePage, Product, ProductVersion
from app.db.base import Base
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.findings.models import Finding, FindingEvidence
from app.identity.models import ApiKey, Membership, Organization, RecoveryCode, User
from app.reports.models import Report
from app.review.models import FieldCorrection, FindingDecision, ReviewSignoff
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
    "dead_letter_jobs",
    "extractions",
    "extracted_fields",
    "evidence_spans",
    "findings",
    "finding_evidence",
    "finding_decisions",
    "field_corrections",
    "review_signoffs",
    "reports",
    "audit_logs",
)

__all__ = [
    "Base",
    "TENANT_TABLES",
    "Analysis",
    "AnalysisEvent",
    "ApiKey",
    "AuditLog",
    "DeadLetterJob",
    "EvidenceSpan",
    "ExtractedField",
    "Extraction",
    "FieldCorrection",
    "File",
    "FilePage",
    "Finding",
    "FindingDecision",
    "FindingEvidence",
    "Membership",
    "OcrResult",
    "OcrTokenRow",
    "Organization",
    "Product",
    "ProductVersion",
    "RecoveryCode",
    "Report",
    "ReviewSignoff",
    "RuleRow",
    "Ruleset",
    "User",
]
