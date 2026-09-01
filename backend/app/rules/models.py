"""Persisted, published rule packs (P4-T4).

Not tenant-scoped, unlike every other table in this codebase so far:
regulatory content is the same for every organization, so `rulesets` and
`rules` carry no `organization_id` and no RLS policy - see
`app.rules.publish` for the full rationale.

`RuleRow.payload` stores the full validated `app.rules.schema.Rule` payload
(`rule.model_dump(mode="json")`) rather than one DB column per field, so
`app.rules.publish.load_ruleset()` can reconstruct the *exact* `Rule` object
that was published via `Rule.model_validate(row.payload)` - the mechanism
behind "re-running an old analysis reproduces its findings byte-for-byte."
The handful of columns duplicated alongside `payload` (rule_key, version,
severity, effective window) exist purely for indexing/querying, not as the
source of truth for reconstruction.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    JSON,
    Date,
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

from app.db.base import Base, UUIDPrimaryKey, utcnow


class Ruleset(UUIDPrimaryKey, Base):
    __tablename__ = "rulesets"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "category", "version", name="uq_rulesets_jur_cat_version"
        ),
        UniqueConstraint("checksum", name="uq_rulesets_checksum"),
        Index("ix_rulesets_jur_cat", "jurisdiction", "category"),
    )

    jurisdiction: Mapped[str] = mapped_column(String(10), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[dt.date | None] = mapped_column(Date, default=None)
    source_citations: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    author: Mapped[str] = mapped_column(String(200), nullable=False)
    review_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )


class RuleRow(UUIDPrimaryKey, Base):
    __tablename__ = "rules"
    __table_args__ = (
        UniqueConstraint("ruleset_id", "rule_key", name="uq_rules_ruleset_rule_key"),
        Index("ix_rules_ruleset", "ruleset_id"),
        Index("ix_rules_rule_key", "rule_key"),
    )

    ruleset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("rulesets.id", ondelete="CASCADE"), nullable=False
    )
    rule_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    citation: Mapped[str] = mapped_column(String(500), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[dt.date | None] = mapped_column(Date, default=None)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
