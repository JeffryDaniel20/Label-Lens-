"""Persisted report snapshots (P7-T1).

A `Report` row is the DB record of one generated snapshot -
IMPLEMENTATION.md section 19's "self-contained immutable snapshot, not a
live query": once generated, `snapshot` never changes, so a report produced
today reads identically a year from now even if the underlying analysis,
findings, or ruleset are later purged under retention (section 19's whole
point is that the snapshot, not a live re-query, is what a compliance
reviewer can point to later). `pdf_key` stays `None` until P7-T2 (PDF
rendering) exists to fill it in - this task only produces the JSON half.

Tenant-scoped and append-only (migration 0013), reusing the same
`labellens_reject_mutation()` trigger every other immutable table in this
codebase already uses.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey, utcnow

# Must match app/findings/models.py's JsonB variant (migration 0009's fix).
JsonB = JSON().with_variant(JSONB(), "postgresql")


class Report(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_org_analysis", "organization_id", "analysis_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    # "json" today; "pdf" is reserved for once P7-T2 renders one from the
    # same snapshot, matching this row's own `pdf_key`.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="json")
    snapshot: Mapped[dict[str, object]] = mapped_column(JsonB, nullable=False)
    pdf_key: Mapped[str | None] = mapped_column(String(400), default=None)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
    # `None` until P6's review/sign-off workflow exists to set it - a report
    # can be generated before sign-off (a draft/working report), so this
    # column is honestly nullable rather than assumed to always be set.
    signed_off_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
