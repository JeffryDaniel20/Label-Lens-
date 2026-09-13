"""Add `files.purged_at` / `file_pages.purged_at` (P7-T8): the retention
purge job deletes only the object-storage bytes behind an expired file, never
the row - `Finding`/`EvidenceSpan`/`Report` rows already carry everything the
pipeline produced, independent of the source image. `purged_at` is how the
job (and the download endpoint) tells "still has its object" from "object
deliberately deleted".

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("files") as batch_op:
        batch_op.add_column(sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("file_pages") as batch_op:
        batch_op.add_column(sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("file_pages") as batch_op:
        batch_op.drop_column("purged_at")
    with op.batch_alter_table("files") as batch_op:
        batch_op.drop_column("purged_at")
