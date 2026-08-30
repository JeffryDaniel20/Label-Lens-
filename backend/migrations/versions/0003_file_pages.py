"""Add the file_pages table (rasterized, EXIF-free page renders of a file).

Row-level security is installed on PostgreSQL, matching 0001/0002. Like
`files`, this table carries no immutability trigger.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def upgrade() -> None:
    op.create_table(
        "file_pages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("render_key", sa.String(400), nullable=False),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("file_id", "page_no", name="uq_file_pages_file_page_no"),
    )
    op.create_index("ix_file_pages_org_file", "file_pages", ["organization_id", "file_id"])

    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE file_pages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE file_pages FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY file_pages_tenant_isolation ON file_pages
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
        op.execute("DROP POLICY IF EXISTS file_pages_tenant_isolation ON file_pages")
        op.execute("ALTER TABLE file_pages DISABLE ROW LEVEL SECURITY")
    op.drop_table("file_pages")
