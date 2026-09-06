"""Add reports (P7-T1): the persisted, immutable JSON report snapshot.

Tenant-scoped (RLS) and append-only, per IMPLEMENTATION.md section 19's "a
report is a self-contained immutable snapshot" - reusing the exact same
`labellens_reject_mutation()` trigger function `audit_logs`/`rulesets`/
`rules`/`analysis_events`/`evidence_spans`/`findings`/`finding_evidence`
already use.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# Must match app/reports/models.py's JsonB variant.
JSONB_VARIANT = sa.JSON().with_variant(JSONB(), "postgresql")

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def upgrade() -> None:
    op.create_table(
        "reports",
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
        sa.Column("kind", sa.String(20), nullable=False, server_default="json"),
        sa.Column("snapshot", JSONB_VARIANT, nullable=False),
        sa.Column("pdf_key", sa.String(400), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        _ts("generated_at", nullable=False, server_default=sa.func.now()),
        sa.Column(
            "signed_off_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_reports_org_analysis", "reports", ["organization_id", "analysis_id"])

    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE reports ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reports FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY reports_tenant_isolation ON reports
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
        """
        CREATE TRIGGER reports_append_only
        BEFORE UPDATE OR DELETE ON reports
        FOR EACH ROW EXECUTE FUNCTION labellens_reject_mutation();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS reports_append_only ON reports")
        op.execute("DROP POLICY IF EXISTS reports_tenant_isolation ON reports")
    op.drop_table("reports")
