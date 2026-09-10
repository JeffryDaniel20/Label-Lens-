"""Add the human review workflow (P6-T5): `finding_decisions` (confirm/
override/escalate a finding, mirroring `app.review.models.DecisionAction`),
`field_corrections` (a reviewer's correction to one extracted field, which
spawns a child analysis rule-evaluated against the corrected fact), and
`analyses.parent_analysis_id` (that child-analysis link,
IMPLEMENTATION.md §12: "the re-evaluation creates a new analysis row
referencing the parent - the original is never edited").

Both new tables are tenant-scoped (RLS) and append-only, reusing the exact
same `labellens_reject_mutation()` trigger function `audit_logs`/
`analysis_events`/`findings`/`finding_evidence`/`evidence_spans`/`rulesets`/
`rules` already use.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def upgrade() -> None:
    op.add_column(
        "analyses",
        sa.Column(
            "parent_analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_table(
        "finding_decisions",
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
            "analysis_id", sa.Uuid(), sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_label", sa.String(200), nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_finding_decisions_org_finding", "finding_decisions", ["organization_id", "finding_id"]
    )

    op.create_table(
        "field_corrections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "analysis_id", sa.Uuid(), sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "child_analysis_id", sa.Uuid(), sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_path", sa.String(100), nullable=False),
        sa.Column("original_value", sa.String(4000), nullable=True),
        sa.Column("corrected_value", sa.String(4000), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_label", sa.String(200), nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_field_corrections_org_analysis", "field_corrections", ["organization_id", "analysis_id"]
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    for table in ("finding_decisions", "field_corrections"):
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
        for table in ("field_corrections", "finding_decisions"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
            op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table("field_corrections")
    op.drop_table("finding_decisions")
    op.drop_column("analyses", "parent_analysis_id")
