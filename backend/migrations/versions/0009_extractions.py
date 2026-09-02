"""Add extractions and extracted_fields tables (P3-T5).

Both tenant-scoped (RLS), following `analyses`/`analysis_events`. Neither is
append-only: IMPLEMENTATION.md §6's immutability list covers `analyses`,
`findings`, `reports`, `audit_logs` and published `rulesets`/`rules` - not
these - and `extracted_fields.value_norm`/`unit` are deliberately filled in
later by the `normalizing` stage (P3-T7), which an append-only trigger would
make impossible.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# Real `jsonb` on PostgreSQL (§6, and a prerequisite for the GIN index
# below), ordinary JSON on SQLite. Must match app/extraction/models.py.
JSONB_VARIANT = sa.JSON().with_variant(JSONB(), "postgresql")

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def upgrade() -> None:
    op.create_table(
        "extractions",
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
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("payload", JSONB_VARIANT, nullable=False),
        sa.Column("envelope", JSONB_VARIANT, nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(20), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_extractions_org_analysis", "extractions", ["organization_id", "analysis_id"]
    )

    op.create_table(
        "extracted_fields",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "extraction_id",
            sa.Uuid(),
            sa.ForeignKey("extractions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_path", sa.String(100), nullable=False),
        sa.Column("value_raw", sa.String(4000), nullable=True),
        sa.Column("not_found_reason", sa.String(500), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cited_token_ids", JSONB_VARIANT, nullable=False),
        sa.Column("value_norm", JSONB_VARIANT, nullable=True),
        sa.Column("unit", sa.String(20), nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_extracted_fields_org_extraction",
        "extracted_fields",
        ["organization_id", "extraction_id"],
    )
    op.create_index(
        "ix_extracted_fields_extraction_path",
        "extracted_fields",
        ["extraction_id", "field_path"],
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    for table in ("extractions", "extracted_fields"):
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

    # GIN on the extraction payload, per §6's representative index list -
    # rule evaluation and debugging both query into the JSON facts.
    op.execute("CREATE INDEX ix_extractions_payload_gin ON extractions USING GIN (payload)")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_extractions_payload_gin")
        for table in ("extractions", "extracted_fields"):
            op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table("extracted_fields")
    op.drop_table("extractions")
