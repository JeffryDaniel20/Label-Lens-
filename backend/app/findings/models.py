"""Persisted rule-evaluation output (P5-T4).

`Finding` is one row per `app.rules.evaluator.Finding` the engine produced for
one analysis - the DB record of a verdict the rule engine is solely
responsible for (IMPLEMENTATION.md's "AI extracts, rules decide"). `rule_id`,
`rule_key`, and `rule_version` are all stored, not just `rule_id`: `rules`
rows are append-only and never edited, but a `rule_key`'s *current* row in
`rules` can still be a different version than the one a historical finding
was actually judged under if the ruleset is later republished with the same
key at a new version - `rule_key`/`rule_version` denormalized here is what
lets a finding be explained without depending on `rules` still holding
exactly that version at read time, the same reasoning `extractions.provider`/
`model` denormalize the LLM identity rather than trusting it stays constant.

`FindingEvidence` is the `finding_id -> extracted_field_id -> evidence_span_id`
edge IMPLEMENTATION.md section 11 calls the evidence chain, materialized as a
real, queryable row per (finding, field, span) triple - a finding backed by
three cited tokens across two fields gets multiple `FindingEvidence` rows,
not one edge with a list buried in JSON, so `GET /findings/{id}/evidence` is
a plain join, not an application-level flattening step.

Both tables are tenant-scoped (unlike `rulesets`/`rules`, which are shared
regulatory content - a finding is specific to one organization's analysis)
and append-only (migration 0012), reusing the same shared
`labellens_reject_mutation()` trigger function every other immutable table in
this codebase already uses.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey, enum_column
from app.rules.evaluator import FindingStatus
from app.rules.schema import Severity

# Same GIN-capable JSON variant `extractions.payload`/`extracted_fields.value_norm`
# already use (migration 0009) - `details` carries a structured, queryable
# `reason`/`evidence_fields` payload, not just an opaque blob.
JsonB = JSON().with_variant(JSONB(), "postgresql")


class Finding(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "findings"
    __table_args__ = (
        Index("ix_findings_org_analysis", "organization_id", "analysis_id"),
        Index("ix_findings_analysis_status_severity", "analysis_id", "status", "severity"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    # No `ondelete`: `rulesets`/`rules` are append-only and never deleted in
    # practice, so this intentionally defaults to NO ACTION rather than
    # cascading a finding's own deletion to what should be an impossible event.
    ruleset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("rulesets.id"), nullable=False
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("rules.id"), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(120), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[FindingStatus] = mapped_column(enum_column(FindingStatus), nullable=False)
    severity: Mapped[Severity] = mapped_column(enum_column(Severity), nullable=False)
    message: Mapped[str | None] = mapped_column(String(1000), default=None)
    # `{"reason": str | None, "evidence_fields": [str, ...]}` - the evaluator's
    # own `Finding.reason` (populated for `not_applicable`/`insufficient_data`)
    # plus the dotted field paths it depended on, kept structured rather than
    # only reachable by re-parsing `message`.
    details: Mapped[dict[str, object]] = mapped_column(JsonB, nullable=False, default=dict)
    # 0.0 for `not_applicable`/`insufficient_data` (nothing was verified for
    # either verdict); otherwise the worst per-field confidence (P3-T8's
    # `app.confidence.tiers`) among this finding's own `evidence_fields` -
    # the "narrow to fields a triggered rule depends on" that P3-T8's own
    # docstring names as P5-T4's job to do, now done.
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class FindingEvidence(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "finding_evidence"
    __table_args__ = (
        Index("ix_finding_evidence_org_finding", "organization_id", "finding_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    extracted_field_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("extracted_fields.id", ondelete="CASCADE"), nullable=False
    )
    evidence_span_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("evidence_spans.id", ondelete="CASCADE"), nullable=False
    )
    # A single fixed value for now ("cited") - IMPLEMENTATION.md names `role`
    # without enumerating values yet; every edge this codebase creates today
    # means the same thing (this span is cited support for this finding), so
    # a real taxonomy (e.g. "primary" vs "corroborating") is deferred until a
    # feature actually needs to tell them apart, matching how `evidence_spans.
    # source` started as "ocr"-only and grows new values as new paths exist.
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="cited")
