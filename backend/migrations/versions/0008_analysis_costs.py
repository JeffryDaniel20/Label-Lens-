"""Add cost-accounting columns to analyses (P5-T5).

IMPLEMENTATION.md §14/§28: "costs are recorded per stage (tokens, provider,
cents) on the analysis row" - three running totals, incremented by
`app.analysis.costs.record_stage_cost` as each stage's provider call
completes, rather than a separate per-stage ledger table.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analyses",
        sa.Column("total_tokens_in", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "analyses",
        sa.Column("total_tokens_out", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "analyses",
        sa.Column("total_cost_cents", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("analyses", "total_cost_cents")
    op.drop_column("analyses", "total_tokens_out")
    op.drop_column("analyses", "total_tokens_in")
