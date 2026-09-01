"""Add analyses and analysis_events tables (P5-T1).

Both tables are tenant-scoped (RLS), unlike `rulesets`/`rules` (P4-T4):
an analysis belongs to one organization. Two different immutability
guarantees, matching how each table is actually meant to change over time:

- `analyses` legitimately gets UPDATEd many times over its life (once per
  state transition) but must never change again once it reaches a terminal
  state (`completed`/`failed`/`cancelled`) - enforced by a conditional
  trigger that inspects `OLD.state`.
- `analysis_events` is unconditionally append-only from the moment a row is
  inserted, reusing the exact same `labellens_reject_mutation()` trigger
  function `audit_logs` already defined in migration 0001.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def upgrade() -> None:
    op.create_table(
        "analyses",
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
        sa.Column("state", sa.String(20), nullable=False, server_default="queued"),
        sa.Column(
            "ruleset_version_id",
            sa.Uuid(),
            sa.ForeignKey("rulesets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model_manifest_id", sa.Uuid(), nullable=True),
        sa.Column("confidence_tier", sa.String(10), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("failure_stage", sa.String(30), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        _ts("started_at", nullable=False, server_default=sa.func.now()),
        _ts("finished_at", nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_analyses_org_idempotency"
        ),
    )
    op.create_index("ix_analyses_org_state", "analyses", ["organization_id", "state"])
    op.create_index(
        "ix_analyses_org_product_version", "analyses", ["organization_id", "product_version_id"]
    )

    op.create_table(
        "analysis_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(20), nullable=True),
        sa.Column("to_state", sa.String(20), nullable=False),
        _ts("occurred_at", nullable=False, server_default=sa.func.now()),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("worker_id", sa.String(100), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.UniqueConstraint(
            "analysis_id", "sequence", name="uq_analysis_events_analysis_sequence"
        ),
    )
    op.create_index(
        "ix_analysis_events_org_analysis", "analysis_events", ["organization_id", "analysis_id"]
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    for table in ("analyses", "analysis_events"):
        op.execute("ALTER TABLE " + table + " ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE " + table + " FORCE ROW LEVEL SECURITY")
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

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_terminal_analysis_mutation() RETURNS trigger AS $$
        BEGIN
            IF OLD.state IN ('completed', 'failed', 'cancelled') THEN
                RAISE EXCEPTION
                    'analysis % is in a terminal state (%) and cannot be modified',
                    OLD.id, OLD.state;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER analyses_terminal_immutable
        BEFORE UPDATE OR DELETE ON analyses
        FOR EACH ROW EXECUTE FUNCTION reject_terminal_analysis_mutation();
        """
    )
    # `analysis_events` reuses the same append-only trigger function
    # `audit_logs`/`rulesets`/`rules` already use.
    op.execute(
        """
        CREATE TRIGGER analysis_events_append_only
        BEFORE UPDATE OR DELETE ON analysis_events
        FOR EACH ROW EXECUTE FUNCTION labellens_reject_mutation();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS analysis_events_append_only ON analysis_events")
        op.execute("DROP TRIGGER IF EXISTS analyses_terminal_immutable ON analyses")
        op.execute("DROP FUNCTION IF EXISTS reject_terminal_analysis_mutation()")
        for table in ("analyses", "analysis_events"):
            op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table("analysis_events")
    op.drop_table("analyses")
