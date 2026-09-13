"""Add `ocr_results.selected` (P3-T3): a page can now genuinely have more
than one `OcrResult` once confidence-triggered escalation runs a fallback
engine alongside the primary one. Exactly one result per page is `selected`
at a time; `app.extraction.service.load_ocr_tokens` reads only the selected
result's tokens so extraction never sees a page's text duplicated across
two engines. Every existing row defaults `True` - a page with only one
`OcrResult` (every page before this task) is trivially "the" selected one.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ocr_results") as batch_op:
        batch_op.add_column(
            sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("ocr_results") as batch_op:
        batch_op.drop_column("selected")
