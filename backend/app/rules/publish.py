"""Ruleset publishing, pinning, and loading (P4-T4).

A ruleset is a *published*, immutable snapshot of a `RulePack` (P4-T1): once
a `(jurisdiction, category, version)` triple is published, its content is
permanently fixed.

- **Idempotent publish**: publishing the exact same pack content again
  (same checksum) is a no-op that returns the already-published `Ruleset`
  row, never a duplicate or an error.
- **Conflicting publish is rejected**: publishing *different* content under
  a `(jurisdiction, category, version)` triple that's already published
  (different checksum) raises `Conflict` outright - a published version
  number is a permanent identity, not something later content can silently
  replace.
- **Database-enforced immutability**: PostgreSQL rejects any `UPDATE`/
  `DELETE` on `rulesets`/`rules` via the same append-only trigger pattern as
  `audit_logs` (migration 0005) - not just an application-level promise.
- **Byte-identical re-evaluation**: an analysis pins a ruleset by
  `ruleset_id`, never "latest". `load_ruleset()` reconstructs the exact
  `Rule` objects that were published, from the exact JSON payload validated
  at publish time (`Rule.model_validate(row.payload)`) - so re-running a
  3-month-old analysis against its pinned ruleset reproduces byte-identical
  findings even if newer rulesets have since been published for the same
  jurisdiction/category.

Not tenant-scoped: regulatory content is the same for every organization,
so `rulesets`/`rules` carry no `organization_id` and no RLS policy, unlike
every other table in this codebase so far.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.errors import Conflict, NotFound
from app.rules.loader import RulePack
from app.rules.models import RuleRow, Ruleset
from app.rules.schema import Rule


def publish_pack(db: Session, pack: RulePack) -> Ruleset:
    """Publish `pack`, or return the already-published `Ruleset` if this
    exact content (by checksum) was published before."""
    existing_by_checksum = db.scalar(select(Ruleset).where(Ruleset.checksum == pack.checksum))
    if existing_by_checksum is not None:
        return existing_by_checksum

    existing_by_identity = db.scalar(
        select(Ruleset).where(
            Ruleset.jurisdiction == pack.manifest.jurisdiction,
            Ruleset.category == pack.manifest.category,
            Ruleset.version == pack.manifest.version,
        )
    )
    if existing_by_identity is not None:
        raise Conflict(
            f"{pack.manifest.pack_id} version {pack.manifest.version} is already published "
            "with different content; publish a new version instead of replacing this one."
        )

    ruleset = Ruleset(
        jurisdiction=pack.manifest.jurisdiction,
        category=pack.manifest.category,
        version=pack.manifest.version,
        effective_from=pack.manifest.effective_from,
        effective_to=pack.manifest.effective_to,
        source_citations=pack.manifest.source_citations,
        author=pack.manifest.author,
        review_date=pack.manifest.review_date,
        checksum=pack.checksum,
    )
    db.add(ruleset)
    db.flush()

    for rule in pack.rules:
        db.add(
            RuleRow(
                ruleset_id=ruleset.id,
                rule_key=rule.rule_key,
                version=rule.version,
                title=rule.title,
                citation=rule.citation,
                severity=rule.severity.value,
                effective_from=rule.effective_from,
                effective_to=rule.effective_to,
                payload=rule.model_dump(mode="json"),
            )
        )
    db.flush()
    return ruleset


def load_ruleset(db: Session, ruleset_id: uuid.UUID) -> tuple[Ruleset, list[Rule]]:
    """Reconstruct the exact, validated `Rule`s published under
    `ruleset_id` - the pinning mechanism byte-identical re-evaluation
    depends on."""
    ruleset = db.get(Ruleset, ruleset_id)
    if ruleset is None:
        raise NotFound("Ruleset not found.")
    rows = db.scalars(select(RuleRow).where(RuleRow.ruleset_id == ruleset_id)).all()
    rules = [Rule.model_validate(row.payload) for row in rows]
    return ruleset, rules
