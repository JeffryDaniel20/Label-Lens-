"""Add rulesets and rules tables (P4-T4): published, immutable rule packs.

Not tenant-scoped, unlike every other table added so far: regulatory content
is the same for every organization, so these carry no `organization_id`
column and no RLS policy. Both tables get the same append-only immutability
trigger already defined for `audit_logs` in 0001
(`labellens_reject_mutation()`) - a published ruleset must never be
editable, at the database level, not just by application convention.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rulesets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("jurisdiction", sa.String(10), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("source_citations", sa.JSON(), nullable=False),
        sa.Column("author", sa.String(200), nullable=False),
        sa.Column("review_date", sa.Date(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "jurisdiction", "category", "version", name="uq_rulesets_jur_cat_version"
        ),
        sa.UniqueConstraint("checksum", name="uq_rulesets_checksum"),
    )
    op.create_index("ix_rulesets_jur_cat", "rulesets", ["jurisdiction", "category"])

    op.create_table(
        "rules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "ruleset_id",
            sa.Uuid(),
            sa.ForeignKey("rulesets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rule_key", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("citation", sa.String(500), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("ruleset_id", "rule_key", name="uq_rules_ruleset_rule_key"),
    )
    op.create_index("ix_rules_ruleset", "rules", ["ruleset_id"])
    op.create_index("ix_rules_rule_key", "rules", ["rule_key"])

    if op.get_bind().dialect.name != "postgresql":
        return

    # `labellens_reject_mutation()` already exists (created in 0001 for
    # audit_logs) - reused here rather than redefined.
    for table in ("rulesets", "rules"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION labellens_reject_mutation();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("rulesets", "rules"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table("rules")
    op.drop_table("rulesets")
