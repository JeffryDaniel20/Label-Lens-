"""Review-workflow persistence (P6-T5): a reviewer's decision on one
finding, and a reviewer's correction to one extracted field.

Both tables are tenant-scoped and append-only (migration 0014), the same
`labellens_reject_mutation()` trigger `audit_logs`/`analysis_events`/
`findings`/`evidence_spans`/`rulesets`/`rules` already use - IMPLEMENTATION.md
§12's own review workflow is explicit that a correction "is a new immutable
revision," never an edit in place, and a decision is exactly the same kind
of append-only fact: if a reviewer changes their mind, that is a *new*
`FindingDecision` row, not an update to the old one - `latest_decision()`
(the newest row per finding) is what a caller reads, mirroring how
`Analysis.state` is a cache of the latest `AnalysisEvent` rather than the
source of truth.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.audit.models import ActorType
from app.db.base import Base, TimestampMixin, UUIDPrimaryKey, enum_column


class DecisionAction(enum.StrEnum):
    CONFIRM = "confirm"
    OVERRIDE = "override"
    ESCALATE = "escalate"


# IMPLEMENTATION.md §12: "Override (status + mandatory reason, min 20 chars)".
OVERRIDE_REASON_MIN_LENGTH = 20


class FindingDecision(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "finding_decisions"
    __table_args__ = (
        Index("ix_finding_decisions_org_finding", "organization_id", "finding_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized for query convenience (the same reasoning
    # `finding_evidence` isn't the only way to reach an evidence span) - a
    # reviewer's queue view lists decisions per analysis without a join
    # through `findings` for every row.
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[DecisionAction] = mapped_column(enum_column(DecisionAction), nullable=False)
    # Required (and length-checked) for `override` at the service layer, not
    # the database - `confirm`/`escalate` never need one, so a `NOT NULL`
    # constraint here would be wrong for two of the three actions.
    reason: Mapped[str | None] = mapped_column(String(2000), default=None)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    actor_type: Mapped[ActorType] = mapped_column(enum_column(ActorType), nullable=False)
    actor_label: Mapped[str | None] = mapped_column(String(200), default=None)


class FieldCorrection(UUIDPrimaryKey, TimestampMixin, Base):
    """A reviewer's correction to one field of one (parent) analysis -
    `child_analysis_id` is the new analysis IMPLEMENTATION.md §12 calls for
    ("the re-evaluation creates a new analysis row referencing the parent -
    the original is never edited"), created and rule-evaluated in the same
    transaction this row is written in (`app.review.service.
    create_field_correction`)."""

    __tablename__ = "field_corrections"
    __table_args__ = (
        Index("ix_field_corrections_org_analysis", "organization_id", "analysis_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # The parent analysis whose extraction is being corrected.
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    child_analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    field_path: Mapped[str] = mapped_column(String(100), nullable=False)
    original_value: Mapped[str | None] = mapped_column(String(4000), default=None)
    corrected_value: Mapped[str] = mapped_column(String(4000), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(2000), default=None)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    actor_type: Mapped[ActorType] = mapped_column(enum_column(ActorType), nullable=False)
    actor_label: Mapped[str | None] = mapped_column(String(200), default=None)
