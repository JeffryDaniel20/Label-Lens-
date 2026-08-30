"""Migration and PostgreSQL-only guarantees.

The migration up/down test runs on SQLite. Row-level security and the
append-only audit trigger exist only on PostgreSQL, so those tests are skipped
unless LABELLENS_TEST_PG_URL points at a live database.
"""

from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.db.models import TENANT_TABLES

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PG_URL = os.environ.get("LABELLENS_TEST_PG_URL")

EXPECTED_TABLES = {
    "organizations",
    "users",
    "recovery_codes",
    "memberships",
    "api_keys",
    "products",
    "product_versions",
    "audit_logs",
}


def _alembic_config(url: str) -> Config:
    config = Config(os.path.join(BACKEND_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(BACKEND_ROOT, "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.mark.integration
def test_migration_upgrade_and_downgrade(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    monkeypatch.setenv("LABELLENS_DATABASE_URL", url)
    config = _alembic_config(url)

    command.upgrade(config, "head")
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables

    command.downgrade(config, "base")
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert remaining == set()


@pytest.mark.integration
def test_migrated_schema_matches_the_models(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every mapped column exists in the migrated schema."""
    from app.db.models import Base

    url = f"sqlite:///{tmp_path / 'compare.db'}"
    monkeypatch.setenv("LABELLENS_DATABASE_URL", url)
    command.upgrade(_alembic_config(url), "head")
    inspector = inspect(create_engine(url))
    for table_name, table in Base.metadata.tables.items():
        actual = {c["name"] for c in inspector.get_columns(table_name)}
        assert {c.name for c in table.columns} <= actual, table_name


@pytest.mark.postgres
@pytest.mark.skipif(not PG_URL, reason="LABELLENS_TEST_PG_URL is not set")
class TestPostgresGuarantees:
    @pytest.fixture
    def pg_engine(self, monkeypatch: pytest.MonkeyPatch):
        assert PG_URL
        monkeypatch.setenv("LABELLENS_DATABASE_URL", PG_URL)
        config = _alembic_config(PG_URL)
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        engine = create_engine(PG_URL)
        yield engine
        command.downgrade(config, "base")

    def test_rls_is_enabled_on_every_tenant_table(self, pg_engine) -> None:
        with pg_engine.connect() as conn:
            for table in TENANT_TABLES:
                enabled = conn.execute(
                    text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
                ).scalar()
                assert enabled is True, f"RLS not enabled on {table}"

    @pytest.fixture
    def unprivileged_role(self, pg_engine):
        """A real, non-superuser login role.

        PostgreSQL's row-level security has a sharp edge: a superuser bypasses
        RLS unconditionally, even on a table with FORCE ROW LEVEL SECURITY. The
        official postgres image's bootstrap user (used by `pg_engine` for DDL
        and by convenience elsewhere in this file) *is* a superuser, so testing
        the tenant-isolation policy through that connection would silently
        prove nothing. This fixture provisions a throwaway, unprivileged role
        so the RLS test exercises what the running application actually gets.
        """
        role = f"ll_unpriv_{uuid.uuid4().hex[:8]}"
        # CREATE ROLE is DDL and does not accept bound parameters; the password
        # is a locally generated hex UUID, so string interpolation here carries
        # no injection risk.
        password = uuid.uuid4().hex
        with pg_engine.begin() as conn:
            conn.execute(text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER"))
            conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            conn.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE "
                    f'ON ALL TABLES IN SCHEMA public TO "{role}"'
                )
            )
        # Built via `make_url(...).set(...)` rather than a literal credential
        # string-replace: a hard-coded `.replace("://labellens:labellens@", ...)`
        # silently no-ops (and falls back to the superuser connection) whenever
        # the live test database doesn't happen to use that exact convention -
        # defeating the entire point of this fixture without ever failing loudly.
        assert PG_URL
        url = make_url(PG_URL).set(username=role, password=password)
        role_engine = create_engine(url)
        try:
            yield role_engine
        finally:
            role_engine.dispose()
            with pg_engine.begin() as conn:
                conn.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"'))
                conn.execute(text(f'REVOKE ALL ON SCHEMA public FROM "{role}"'))
                conn.execute(text(f'DROP ROLE "{role}"'))

    def test_rls_blocks_rows_from_another_tenant(self, pg_engine, unprivileged_role) -> None:
        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        with pg_engine.begin() as conn:
            for org_id, name in ((org_a, "A"), (org_b, "B")):
                conn.execute(
                    text(
                        "INSERT INTO organizations (id, name, slug, retention_days,"
                        " cloud_ai_enabled, created_at, updated_at)"
                        " VALUES (:id, :n, :s, 365, true, now(), now())"
                    ),
                    {"id": org_id, "n": name, "s": name.lower()},
                )
                conn.execute(
                    text(
                        "INSERT INTO products (id, organization_id, name, internal_sku,"
                        " created_at, updated_at) VALUES (:id, :org, :n, :sku, now(), now())"
                    ),
                    {"id": uuid.uuid4(), "org": org_id, "n": f"{name} product", "sku": f"{name}-1"},
                )
        # Connect as the unprivileged role: this is what actually proves RLS
        # enforces isolation, rather than a superuser connection that bypasses
        # the policy regardless of whether it is correctly defined.
        with unprivileged_role.connect() as conn:
            conn.execute(text("SELECT set_config('app.org_id', :o, false)"), {"o": str(org_a)})
            rows = conn.execute(text("SELECT organization_id FROM products")).scalars().all()
            assert rows == [org_a]

    def test_superuser_bypasses_rls_a_known_postgres_limitation(self, pg_engine) -> None:
        """Documents the limitation the fixture above works around.

        The application's own database connection must never be a superuser
        or RLS provides no protection at all; see docs/adr/0002 and the
        production runbook.
        """
        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        with pg_engine.begin() as conn:
            for org_id, name in ((org_a, "A"), (org_b, "B")):
                conn.execute(
                    text(
                        "INSERT INTO organizations (id, name, slug, retention_days,"
                        " cloud_ai_enabled, created_at, updated_at)"
                        " VALUES (:id, :n, :s, 365, true, now(), now())"
                    ),
                    {"id": org_id, "n": name, "s": name.lower()},
                )
                conn.execute(
                    text(
                        "INSERT INTO products (id, organization_id, name, internal_sku,"
                        " created_at, updated_at) VALUES (:id, :org, :n, :sku, now(), now())"
                    ),
                    {"id": uuid.uuid4(), "org": org_id, "n": f"{name} product", "sku": f"{name}-1"},
                )
        with pg_engine.connect() as conn:
            is_super = conn.execute(
                text("SELECT usesuper FROM pg_user WHERE usename = current_user")
            ).scalar()
            assert is_super is True
            conn.execute(text("SELECT set_config('app.org_id', :o, false)"), {"o": str(org_a)})
            rows = conn.execute(text("SELECT organization_id FROM products")).scalars().all()
            assert len(rows) == 2, (
                "a superuser connection bypasses RLS by design - never use one in the app"
            )

    def test_audit_log_rejects_update_and_delete(self, pg_engine) -> None:
        from sqlalchemy.exc import DatabaseError

        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO audit_logs (id, actor_type, action, created_at)"
                    " VALUES (:id, 'system', 'test.event', now())"
                ),
                {"id": uuid.uuid4()},
            )
        for statement in (
            "UPDATE audit_logs SET action = 'tampered'",
            "DELETE FROM audit_logs",
        ):
            with pytest.raises(DatabaseError), pg_engine.begin() as conn:
                conn.execute(text(statement))
