"""Add the review queue and sign-off action (P6-T6): `analyses.
assigned_reviewer_id` (who is working an analysis) and `review_signoffs`
(the one, permanent record that a review is done - IMPLEMENTATION.md §12:
"records actor, timestamp, ruleset version, and a hash of the finding
set").

`review_signoffs` is tenant-scoped (RLS), append-only (the same
`labellens_reject_mutation()` trigger every other immutable table in this
codebase uses), and carries a real unique constraint on `analysis_id` -
append-only alone stops an *edit*, but only the uniqueness constraint stops
a *second* sign-off from ever being inserted for the same analysis.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def upgrade() -> None:
    # `batch_alter_table`, not a plain `add_column` - see migration 0014's
    # own note: SQLite cannot `ALTER TABLE ... ADD COLUMN` with a new
    # foreign-key constraint in a single step.
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.add_column(sa.Column("assigned_reviewer_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_analyses_assigned_reviewer_id_users",
            "users",
            ["assigned_reviewer_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "review_signoffs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "analysis_id", sa.Uuid(), sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ruleset_version_id", sa.Uuid(), sa.ForeignKey("rulesets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("finding_set_hash", sa.String(64), nullable=False),
        _ts("signed_off_at", nullable=False, server_default=sa.func.now()),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_label", sa.String(200), nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("analysis_id", name="uq_review_signoffs_analysis_id"),
    )
    op.create_index(
        "ix_review_signoffs_org_analysis", "review_signoffs", ["organization_id", "analysis_id"]
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE review_signoffs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE review_signoffs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY review_signoffs_tenant_isolation ON review_signoffs
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
        CREATE TRIGGER review_signoffs_append_only
        BEFORE UPDATE OR DELETE ON review_signoffs
        FOR EACH ROW EXECUTE FUNCTION labellens_reject_mutation();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS review_signoffs_append_only ON review_signoffs")
        op.execute("DROP POLICY IF EXISTS review_signoffs_tenant_isolation ON review_signoffs")
    op.drop_table("review_signoffs")
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.drop_constraint("fk_analyses_assigned_reviewer_id_users", type_="foreignkey")
        batch_op.drop_column("assigned_reviewer_id")
