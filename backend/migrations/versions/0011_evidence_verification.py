"""Add evidence verification (P3-T6): verified/match_ratio on
extracted_fields, verified/demoted counts on extractions, and the new
evidence_spans table.

`evidence_spans` is tenant-scoped (RLS) and, per IMPLEMENTATION.md §11
("Evidence is immutable"), append-only - reusing the exact same
`labellens_reject_mutation()` trigger function `audit_logs`/`rulesets`/
`rules`/`analysis_events` already use, rather than redefining it.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# Must match app/extraction/models.py's JsonB variant.
JSONB_VARIANT = sa.JSON().with_variant(JSONB(), "postgresql")

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def upgrade() -> None:
    op.add_column(
        "extracted_fields", sa.Column("verified", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "extracted_fields", sa.Column("match_ratio", sa.Float(), nullable=True)
    )
    op.add_column(
        "extractions",
        sa.Column("verified_field_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "extractions",
        sa.Column("demoted_field_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "evidence_spans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "extracted_field_id",
            sa.Uuid(),
            sa.ForeignKey("extracted_fields.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_page_id",
            sa.Uuid(),
            sa.ForeignKey("file_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_ids", JSONB_VARIANT, nullable=False),
        sa.Column("x1", sa.Float(), nullable=False),
        sa.Column("y1", sa.Float(), nullable=False),
        sa.Column("x2", sa.Float(), nullable=False),
        sa.Column("y2", sa.Float(), nullable=False),
        sa.Column("text_snippet", sa.String(2000), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="ocr"),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_evidence_spans_org_field", "evidence_spans", ["organization_id", "extracted_field_id"]
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE evidence_spans ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE evidence_spans FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY evidence_spans_tenant_isolation ON evidence_spans
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
        CREATE TRIGGER evidence_spans_append_only
        BEFORE UPDATE OR DELETE ON evidence_spans
        FOR EACH ROW EXECUTE FUNCTION labellens_reject_mutation();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS evidence_spans_append_only ON evidence_spans")
        op.execute(
            "DROP POLICY IF EXISTS evidence_spans_tenant_isolation ON evidence_spans"
        )
    op.drop_table("evidence_spans")
    op.drop_column("extractions", "demoted_field_count")
    op.drop_column("extractions", "verified_field_count")
    op.drop_column("extracted_fields", "match_ratio")
    op.drop_column("extracted_fields", "verified")
