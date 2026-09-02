"""Analysis entity and state machine persistence (P5-T1).

`Analysis` rows progress through the fixed state machine in
IMPLEMENTATION.md §14; `AnalysisEvent` rows are the append-only transition
log that makes "state history is fully reconstructable from events"
literally true - `Analysis.state` is a convenience cache of the latest
event, never the sole source of truth (`app.analysis.service.reconstruct_state`
derives it purely from events, independent of that column).

PostgreSQL enforces the terminal-state guarantee at the database level, not
just in `app.analysis.state_machine`: once `state` is `completed`, `failed`,
or `cancelled`, any further `UPDATE`/`DELETE` on that `analyses` row is
rejected (migration 0006); `analysis_events` rows are unconditionally
append-only, reusing the same `labellens_reject_mutation()` trigger function
`audit_logs` already defined.

`model_manifest_id` is a plain UUID with no foreign key: `model_manifests`
(IMPLEMENTATION.md §5) has no dedicated table or task yet in this codebase -
nothing before Phase 3's LLM extraction (still blocked on an API credential)
has anything meaningful to put in one. It's carried here as an identifier
placeholder so the column exists when that table does.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey, enum_column, utcnow


class AnalysisState(enum.StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    PREPROCESSING = "preprocessing"
    OCR = "ocr"
    EXTRACTING = "extracting"
    NORMALIZING = "normalizing"
    CLASSIFYING = "classifying"
    RULE_EVAL = "rule_eval"
    SCORING = "scoring"
    NEEDS_REVIEW = "needs_review"
    REVIEW = "review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES: frozenset[AnalysisState] = frozenset(
    {AnalysisState.COMPLETED, AnalysisState.FAILED, AnalysisState.CANCELLED}
)


class ConfidenceTier(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DeadLetterReason(enum.StrEnum):
    STAGE_EXHAUSTED = "stage_exhausted"
    PERMANENT_ERROR = "permanent_error"
    TIMEOUT = "timeout"
    STALLED = "stalled"


class Analysis(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "analyses"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_analyses_org_idempotency"
        ),
        Index("ix_analyses_org_state", "organization_id", "state"),
        Index("ix_analyses_org_product_version", "organization_id", "product_version_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    product_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("product_versions.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[AnalysisState] = mapped_column(
        enum_column(AnalysisState), nullable=False, default=AnalysisState.QUEUED
    )
    ruleset_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("rulesets.id", ondelete="SET NULL"), default=None
    )
    model_manifest_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    confidence_tier: Mapped[ConfidenceTier | None] = mapped_column(
        enum_column(ConfidenceTier), default=None
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_stage: Mapped[str | None] = mapped_column(String(30), default=None)
    retryable: Mapped[bool | None] = mapped_column(Boolean, default=None)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    # Cost accounting (P5-T5): summed across every stage's provider call via
    # `app.analysis.costs.record_stage_cost` - IMPLEMENTATION.md §14/§28's
    # "costs are recorded per stage (tokens, provider, cents) on the
    # analysis row" taken literally rather than as a separate ledger table.
    total_tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AnalysisEvent(UUIDPrimaryKey, Base):
    __tablename__ = "analysis_events"
    __table_args__ = (
        UniqueConstraint("analysis_id", "sequence", name="uq_analysis_events_analysis_sequence"),
        Index("ix_analysis_events_org_analysis", "organization_id", "analysis_id"),
    )

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # A per-analysis, application-assigned ordinal (1, 2, 3, ...) - the
    # authoritative event order. Wall-clock timestamps alone are not safe
    # for this: two transitions committed within the same clock tick would
    # be unorderable by `occurred_at` on some platforms/interpreters.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[AnalysisState | None] = mapped_column(
        enum_column(AnalysisState), default=None
    )
    to_state: Mapped[AnalysisState] = mapped_column(enum_column(AnalysisState), nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    worker_id: Mapped[str | None] = mapped_column(String(100), default=None)
    reason: Mapped[str | None] = mapped_column(String(500), default=None)


class DeadLetterJob(UUIDPrimaryKey, TimestampMixin, Base):
    """A stage job that will not be automatically retried further (P5-T3):
    either its retries were exhausted, it failed permanently, it exceeded
    the whole-analysis time budget, or the janitor reaped it as stalled.
    This row *is* the alert signal for now - a real notification channel
    (P7-T5) can be layered on top of "a new row appeared here" without any
    change to how it's written."""

    __tablename__ = "dead_letter_jobs"
    __table_args__ = (
        Index("ix_dead_letter_jobs_org_analysis", "organization_id", "analysis_id"),
    )

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[DeadLetterReason] = mapped_column(enum_column(DeadLetterReason), nullable=False)
    error_message: Mapped[str] = mapped_column(String(2000), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    replayed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    replayed_as_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("analyses.id", ondelete="SET NULL"), default=None
    )
