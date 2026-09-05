"""Add classification-result columns to analyses (vertical-slice wiring).

`rule_eval` (still a placeholder, blocked on D-01) needs a jurisdiction and
category to pick a ruleset; wiring the `classifying` stage for real means
that result has to live somewhere. IMPLEMENTATION.md §6's `analyses` column
list predates this stage being wired, so these are a genuine addition
rather than filling in an already-planned column.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("category", sa.String(80), nullable=True))
    op.add_column(
        "analyses",
        sa.Column("jurisdictions", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "analyses",
        sa.Column(
            "category_confidence", sa.Float(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "analyses",
        sa.Column(
            "jurisdiction_confidence", sa.Float(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("analyses", "jurisdiction_confidence")
    op.drop_column("analyses", "category_confidence")
    op.drop_column("analyses", "jurisdictions")
    op.drop_column("analyses", "category")
