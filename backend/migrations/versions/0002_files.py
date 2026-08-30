"""Add the files table (validated uploads attached to a product version).

Row-level security is installed for this table on PostgreSQL, matching the
pattern established in 0001. `files` carries no immutability trigger - unlike
analyses or audit rows it can be legitimately superseded (a rejected upload is
just discarded), so ordinary UPDATE/DELETE stay available to the application.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_version_id",
            sa.Uuid(),
            sa.ForeignKey("product_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(400), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="validating"),
        sa.Column("av_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("rejection_reason", sa.String(500), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_files_org_product_version", "files", ["organization_id", "product_version_id"]
    )
    op.create_index("ix_files_org_sha256", "files", ["organization_id", "sha256"])
    op.create_index("ix_files_org_storage_key", "files", ["organization_id", "storage_key"])

    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE files FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY files_tenant_isolation ON files
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
        op.execute("DROP POLICY IF EXISTS files_tenant_isolation ON files")
        op.execute("ALTER TABLE files DISABLE ROW LEVEL SECURITY")
    op.drop_table("files")
