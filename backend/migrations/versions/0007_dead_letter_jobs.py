"""Add dead_letter_jobs table (P5-T3).

Tenant-scoped (RLS), like `analyses`/`analysis_events` - a dead-letter row
belongs to one organization. Unlike either of those, this table is *not*
append-only or conditionally-immutable: replaying a dead-lettered job
legitimately UPDATEs `replayed_at`/`replayed_as_analysis_id` on the same
row, and that update must always be allowed, not just before some terminal
state is reached.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def upgrade() -> None:
    op.create_table(
        "dead_letter_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "analysis_id", sa.Uuid(), sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("error_message", sa.String(2000), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        _ts("replayed_at", nullable=True),
        sa.Column(
            "replayed_as_analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_dead_letter_jobs_org_analysis", "dead_letter_jobs", ["organization_id", "analysis_id"]
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE dead_letter_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dead_letter_jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY dead_letter_jobs_tenant_isolation ON dead_letter_jobs
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


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP POLICY IF EXISTS dead_letter_jobs_tenant_isolation ON dead_letter_jobs"
        )
    op.drop_table("dead_letter_jobs")
