"""Add ocr_results and ocr_tokens (P3-T2 OCR token persistence).

On PostgreSQL, `ocr_tokens` additionally gets a generated `bbox_box` column
(a native `box`, computed from x1/y1/x2/y2) with a GiST index, so tokens are
genuinely spatially queryable - not just filterable by float range - matching
IMPLEMENTATION.md's "GIN/GiST for spatial lookup" note. SQLite has no `box`
type or GiST support, so that part is skipped there, same as RLS.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def _rls(table: str) -> None:
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


def upgrade() -> None:
    op.create_table(
        "ocr_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_page_id",
            sa.Uuid(),
            sa.ForeignKey("file_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("engine", sa.String(50), nullable=False),
        sa.Column("engine_version", sa.String(50), nullable=False),
        sa.Column("avg_confidence", sa.Float(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_ocr_results_org_file_page", "ocr_results", ["organization_id", "file_page_id"]
    )

    op.create_table(
        "ocr_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_page_id",
            sa.Uuid(),
            sa.ForeignKey("file_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ocr_result_id",
            sa.Uuid(),
            sa.ForeignKey("ocr_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("text", sa.String(500), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("x1", sa.Float(), nullable=False),
        sa.Column("y1", sa.Float(), nullable=False),
        sa.Column("x2", sa.Float(), nullable=False),
        sa.Column("y2", sa.Float(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(10), nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_ocr_tokens_org_file_page", "ocr_tokens", ["organization_id", "file_page_id"]
    )
    op.create_index("ix_ocr_tokens_result", "ocr_tokens", ["ocr_result_id"])

    if op.get_bind().dialect.name != "postgresql":
        return

    _rls("ocr_results")
    _rls("ocr_tokens")
    op.execute(
        "ALTER TABLE ocr_tokens ADD COLUMN bbox_box box "
        "GENERATED ALWAYS AS (box(point(x1, y1), point(x2, y2))) STORED"
    )
    op.execute("CREATE INDEX ix_ocr_tokens_bbox_gist ON ocr_tokens USING gist (bbox_box)")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_ocr_tokens_bbox_gist")
        op.execute("DROP POLICY IF EXISTS ocr_tokens_tenant_isolation ON ocr_tokens")
        op.execute("DROP POLICY IF EXISTS ocr_results_tenant_isolation ON ocr_results")
        op.execute("ALTER TABLE ocr_tokens DISABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE ocr_results DISABLE ROW LEVEL SECURITY")
    op.drop_table("ocr_tokens")
    op.drop_table("ocr_results")
