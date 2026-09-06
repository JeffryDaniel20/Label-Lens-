"""Add findings + finding_evidence (P5-T4): the rule engine's persisted
verdicts and the evidence chain edge that traces each one back to real
`extracted_fields`/`evidence_spans` rows.

Both tables are tenant-scoped (RLS) and, per IMPLEMENTATION.md §6
("findings ... immutable"), append-only - reusing the exact same
`labellens_reject_mutation()` trigger function `audit_logs`/`rulesets`/
`rules`/`analysis_events`/`evidence_spans` already use.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# Must match app/findings/models.py's JsonB variant.
JSONB_VARIANT = sa.JSON().with_variant(JSONB(), "postgresql")

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def upgrade() -> None:
    op.create_table(
        "findings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ruleset_id", sa.Uuid(), sa.ForeignKey("rulesets.id"), nullable=False),
        sa.Column("rule_id", sa.Uuid(), sa.ForeignKey("rules.id"), nullable=False),
        sa.Column("rule_key", sa.String(120), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("message", sa.String(1000), nullable=True),
        sa.Column("details", JSONB_VARIANT, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_findings_org_analysis", "findings", ["organization_id", "analysis_id"])
    op.create_index(
        "ix_findings_analysis_status_severity", "findings", ["analysis_id", "status", "severity"]
    )

    op.create_table(
        "finding_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "finding_id", sa.Uuid(), sa.ForeignKey("findings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "extracted_field_id", sa.Uuid(),
            sa.ForeignKey("extracted_fields.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "evidence_span_id", sa.Uuid(),
            sa.ForeignKey("evidence_spans.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False, server_default="cited"),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_finding_evidence_org_finding", "finding_evidence", ["organization_id", "finding_id"]
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    for table in ("findings", "finding_evidence"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (
                organization_id::text = current_setting('app.org_id', true)
                OR coalesce(current_setting('app.org_id', true), '') = ''
            )
            WITH CHECK (
                organization_id::text = current_setting('app.org_id', true)
                OR coalesce(current_setting('app.org_id', true), '') = ''
            )
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION labellens_reject_mutation();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("finding_evidence", "findings"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
            op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table("finding_evidence")
    op.drop_table("findings")
