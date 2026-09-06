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

    def test_rulesets_and_rules_reject_update_and_delete(self, pg_engine) -> None:
        """P4-T4: a published ruleset must never be editable, at the
        database level - not just by application convention (unlike
        `rulesets`/`rules`, these two tables carry no `organization_id` and
        no RLS policy at all, since regulatory content is shared across
        every organization; see app.rules.publish)."""
        from sqlalchemy.exc import DatabaseError

        ruleset_id = uuid.uuid4()
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO rulesets (id, jurisdiction, category, version,"
                    " effective_from, source_citations, author, review_date,"
                    " checksum, published_at)"
                    " VALUES (:id, 'IN', 'packaged_food', '1.0.0', '2024-01-01',"
                    " '[]', 'test', '2024-01-01', 'deadbeef', now())"
                ),
                {"id": ruleset_id},
            )
            conn.execute(
                text(
                    "INSERT INTO rules (id, ruleset_id, rule_key, version, title, citation,"
                    " severity, effective_from, payload)"
                    " VALUES (:id, :ruleset_id, 'IN-TEST', 1, 't', 'c', 'minor',"
                    " '2024-01-01', '{}')"
                ),
                {"id": uuid.uuid4(), "ruleset_id": ruleset_id},
            )
        for statement in (
            "UPDATE rulesets SET version = '2.0.0'",
            "DELETE FROM rulesets",
            "UPDATE rules SET title = 'tampered'",
            "DELETE FROM rules",
        ):
            with pytest.raises(DatabaseError), pg_engine.begin() as conn:
                conn.execute(text(statement))

    def test_analysis_events_are_append_only(self, pg_engine) -> None:
        """P5-T1: state history must be reconstructable from events, which
        requires events to be genuinely immutable once written - the same
        `labellens_reject_mutation()` trigger function as `audit_logs`."""
        from sqlalchemy.exc import DatabaseError

        org_id, product_id, version_id, analysis_id = (uuid.uuid4() for _ in range(4))
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, retention_days,"
                    " cloud_ai_enabled, created_at, updated_at)"
                    " VALUES (:id, 'Org', 'org', 365, true, now(), now())"
                ),
                {"id": org_id},
            )
            conn.execute(
                text(
                    "INSERT INTO products (id, organization_id, name, internal_sku,"
                    " created_at, updated_at) VALUES (:id, :org, 'P', 'S1', now(), now())"
                ),
                {"id": product_id, "org": org_id},
            )
            conn.execute(
                text(
                    "INSERT INTO product_versions (id, organization_id, product_id,"
                    " version_no, label, status, created_at, updated_at)"
                    " VALUES (:id, :org, :p, 1, '', 'draft', now(), now())"
                ),
                {"id": version_id, "org": org_id, "p": product_id},
            )
            conn.execute(
                text(
                    "INSERT INTO analyses (id, organization_id, product_version_id, state,"
                    " idempotency_key, started_at, created_at, updated_at)"
                    " VALUES (:id, :org, :pv, 'queued', 'x', now(), now(), now())"
                ),
                {"id": analysis_id, "org": org_id, "pv": version_id},
            )
            conn.execute(
                text(
                    "INSERT INTO analysis_events (id, analysis_id, organization_id, sequence,"
                    " to_state, occurred_at) VALUES (:id, :aid, :org, 1, 'queued', now())"
                ),
                {"id": uuid.uuid4(), "aid": analysis_id, "org": org_id},
            )
        for statement in (
            "UPDATE analysis_events SET to_state = 'tampered'",
            "DELETE FROM analysis_events",
        ):
            with pytest.raises(DatabaseError), pg_engine.begin() as conn:
                conn.execute(text(statement))

    def test_evidence_spans_are_append_only(self, pg_engine) -> None:
        """P3-T6: evidence backing a verified field must be immutable once
        recorded, the same `labellens_reject_mutation()` trigger function as
        `audit_logs`/`analysis_events`."""
        from sqlalchemy.exc import DatabaseError

        (
            org_id, product_id, version_id, file_id, page_id,
            analysis_id, extraction_id, field_id, span_id,
        ) = (uuid.uuid4() for _ in range(9))
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, retention_days,"
                    " cloud_ai_enabled, created_at, updated_at)"
                    " VALUES (:id, 'Org', 'org', 365, true, now(), now())"
                ),
                {"id": org_id},
            )
            conn.execute(
                text(
                    "INSERT INTO products (id, organization_id, name, internal_sku,"
                    " created_at, updated_at) VALUES (:id, :org, 'P', 'S1', now(), now())"
                ),
                {"id": product_id, "org": org_id},
            )
            conn.execute(
                text(
                    "INSERT INTO product_versions (id, organization_id, product_id,"
                    " version_no, label, status, created_at, updated_at)"
                    " VALUES (:id, :org, :p, 1, '', 'draft', now(), now())"
                ),
                {"id": version_id, "org": org_id, "p": product_id},
            )
            conn.execute(
                text(
                    "INSERT INTO files (id, organization_id, product_version_id, storage_key,"
                    " original_filename, sha256, mime, bytes, status, av_status,"
                    " created_at, updated_at)"
                    " VALUES (:id, :org, :pv, 'k', 'f.jpg', :sha, 'image/jpeg', 1,"
                    " 'processed', 'clean', now(), now())"
                ),
                {"id": file_id, "org": org_id, "pv": version_id, "sha": "0" * 64},
            )
            conn.execute(
                text(
                    "INSERT INTO file_pages (id, organization_id, file_id, page_no,"
                    " width, height, render_key, created_at, updated_at)"
                    " VALUES (:id, :org, :f, 1, 100, 100, 'r', now(), now())"
                ),
                {"id": page_id, "org": org_id, "f": file_id},
            )
            conn.execute(
                text(
                    "INSERT INTO analyses (id, organization_id, product_version_id, state,"
                    " idempotency_key, started_at, created_at, updated_at)"
                    " VALUES (:id, :org, :pv, 'queued', 'x', now(), now(), now())"
                ),
                {"id": analysis_id, "org": org_id, "pv": version_id},
            )
            conn.execute(
                text(
                    "INSERT INTO extractions (id, organization_id, analysis_id,"
                    " schema_version, payload, envelope, provider, model,"
                    " prompt_version, prompt_hash, created_at, updated_at)"
                    " VALUES (:id, :org, :aid, '1', '{}', '{}', 'stub', 'stub',"
                    " '1', 'h', now(), now())"
                ),
                {"id": extraction_id, "org": org_id, "aid": analysis_id},
            )
            conn.execute(
                text(
                    "INSERT INTO extracted_fields (id, organization_id, extraction_id,"
                    " field_path, value_raw, confidence, cited_token_ids,"
                    " created_at, updated_at)"
                    " VALUES (:id, :org, :ext, 'quantity.net_quantity', '250 g', 0.9,"
                    " '[]', now(), now())"
                ),
                {"id": field_id, "org": org_id, "ext": extraction_id},
            )
            conn.execute(
                text(
                    "INSERT INTO evidence_spans (id, organization_id, extracted_field_id,"
                    " file_page_id, token_ids, x1, y1, x2, y2, text_snippet, source,"
                    " created_at, updated_at)"
                    " VALUES (:id, :org, :field, :page, '[]', 0, 0, 10, 10, '250 g',"
                    " 'ocr', now(), now())"
                ),
                {"id": span_id, "org": org_id, "field": field_id, "page": page_id},
            )
        for statement in (
            "UPDATE evidence_spans SET text_snippet = 'tampered'",
            "DELETE FROM evidence_spans",
        ):
            with pytest.raises(DatabaseError), pg_engine.begin() as conn:
                conn.execute(text(statement))

    def test_findings_and_finding_evidence_are_append_only(self, pg_engine) -> None:
        """P5-T4: a rule engine verdict, and the evidence edge tracing it,
        must be immutable once recorded - the same `labellens_reject_
        mutation()` trigger function as `audit_logs`/`analysis_events`/
        `evidence_spans`."""
        from sqlalchemy.exc import DatabaseError

        (
            org_id, product_id, version_id, file_id, page_id, analysis_id, extraction_id,
            field_id, span_id, ruleset_id, rule_id, finding_id, finding_evidence_id,
        ) = (uuid.uuid4() for _ in range(13))
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, retention_days,"
                    " cloud_ai_enabled, created_at, updated_at)"
                    " VALUES (:id, 'Org', 'org', 365, true, now(), now())"
                ),
                {"id": org_id},
            )
            conn.execute(
                text(
                    "INSERT INTO products (id, organization_id, name, internal_sku,"
                    " created_at, updated_at) VALUES (:id, :org, 'P', 'S1', now(), now())"
                ),
                {"id": product_id, "org": org_id},
            )
            conn.execute(
                text(
                    "INSERT INTO product_versions (id, organization_id, product_id,"
                    " version_no, label, status, created_at, updated_at)"
                    " VALUES (:id, :org, :p, 1, '', 'draft', now(), now())"
                ),
                {"id": version_id, "org": org_id, "p": product_id},
            )
            conn.execute(
                text(
                    "INSERT INTO files (id, organization_id, product_version_id, storage_key,"
                    " original_filename, sha256, mime, bytes, status, av_status,"
                    " created_at, updated_at)"
                    " VALUES (:id, :org, :pv, 'k', 'f.jpg', :sha, 'image/jpeg', 1,"
                    " 'processed', 'clean', now(), now())"
                ),
                {"id": file_id, "org": org_id, "pv": version_id, "sha": "1" * 64},
            )
            conn.execute(
                text(
                    "INSERT INTO file_pages (id, organization_id, file_id, page_no,"
                    " width, height, render_key, created_at, updated_at)"
                    " VALUES (:id, :org, :f, 1, 100, 100, 'r', now(), now())"
                ),
                {"id": page_id, "org": org_id, "f": file_id},
            )
            conn.execute(
                text(
                    "INSERT INTO analyses (id, organization_id, product_version_id, state,"
                    " idempotency_key, started_at, created_at, updated_at)"
                    " VALUES (:id, :org, :pv, 'queued', 'x', now(), now(), now())"
                ),
                {"id": analysis_id, "org": org_id, "pv": version_id},
            )
            conn.execute(
                text(
                    "INSERT INTO extractions (id, organization_id, analysis_id,"
                    " schema_version, payload, envelope, provider, model,"
                    " prompt_version, prompt_hash, created_at, updated_at)"
                    " VALUES (:id, :org, :aid, '1', '{}', '{}', 'stub', 'stub',"
                    " '1', 'h', now(), now())"
                ),
                {"id": extraction_id, "org": org_id, "aid": analysis_id},
            )
            conn.execute(
                text(
                    "INSERT INTO extracted_fields (id, organization_id, extraction_id,"
                    " field_path, value_raw, confidence, cited_token_ids,"
                    " created_at, updated_at)"
                    " VALUES (:id, :org, :ext, 'quantity.net_quantity', '250 g', 0.9,"
                    " '[]', now(), now())"
                ),
                {"id": field_id, "org": org_id, "ext": extraction_id},
            )
            conn.execute(
                text(
                    "INSERT INTO evidence_spans (id, organization_id, extracted_field_id,"
                    " file_page_id, token_ids, x1, y1, x2, y2, text_snippet, source,"
                    " created_at, updated_at)"
                    " VALUES (:id, :org, :field, :page, '[]', 0, 0, 10, 10, '250 g',"
                    " 'ocr', now(), now())"
                ),
                {"id": span_id, "org": org_id, "field": field_id, "page": page_id},
            )
            conn.execute(
                text(
                    "INSERT INTO rulesets (id, jurisdiction, category, version,"
                    " effective_from, source_citations, author, review_date,"
                    " checksum, published_at)"
                    " VALUES (:id, 'IN', 'packaged_food', '1.0.0', '2024-01-01',"
                    " '[]', 'test', '2024-01-01', :checksum, now())"
                ),
                {"id": ruleset_id, "checksum": uuid.uuid4().hex},
            )
            conn.execute(
                text(
                    "INSERT INTO rules (id, ruleset_id, rule_key, version, title, citation,"
                    " severity, effective_from, payload)"
                    " VALUES (:id, :ruleset, 'IN-TEST', 1, 't', 'c', 'minor',"
                    " '2024-01-01', '{}')"
                ),
                {"id": rule_id, "ruleset": ruleset_id},
            )
            conn.execute(
                text(
                    "INSERT INTO findings (id, organization_id, analysis_id, ruleset_id,"
                    " rule_id, rule_key, rule_version, status, severity, details,"
                    " confidence, created_at, updated_at)"
                    " VALUES (:id, :org, :aid, :ruleset, :rule, 'IN-TEST', 1, 'pass',"
                    " 'minor', '{}', 0.9, now(), now())"
                ),
                {
                    "id": finding_id, "org": org_id, "aid": analysis_id,
                    "ruleset": ruleset_id, "rule": rule_id,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO finding_evidence (id, organization_id, finding_id,"
                    " extracted_field_id, evidence_span_id, role, created_at, updated_at)"
                    " VALUES (:id, :org, :finding, :field, :span, 'cited', now(), now())"
                ),
                {
                    "id": finding_evidence_id, "org": org_id, "finding": finding_id,
                    "field": field_id, "span": span_id,
                },
            )
        for statement in (
            "UPDATE finding_evidence SET role = 'tampered'",
            "DELETE FROM finding_evidence",
        ):
            with pytest.raises(DatabaseError), pg_engine.begin() as conn:
                conn.execute(text(statement))
        for statement in (
            "UPDATE findings SET status = 'fail'",
            "DELETE FROM findings",
        ):
            with pytest.raises(DatabaseError), pg_engine.begin() as conn:
                conn.execute(text(statement))

    def test_reports_are_append_only(self, pg_engine) -> None:
        """P7-T1: a generated report snapshot must be immutable once
        recorded - section 19's "self-contained immutable snapshot" -
        reusing the same `labellens_reject_mutation()` trigger function as
        every other immutable table in this codebase."""
        from sqlalchemy.exc import DatabaseError

        org_id, product_id, version_id, analysis_id, report_id = (uuid.uuid4() for _ in range(5))
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, retention_days,"
                    " cloud_ai_enabled, created_at, updated_at)"
                    " VALUES (:id, 'Org', 'org', 365, true, now(), now())"
                ),
                {"id": org_id},
            )
            conn.execute(
                text(
                    "INSERT INTO products (id, organization_id, name, internal_sku,"
                    " created_at, updated_at) VALUES (:id, :org, 'P', 'S1', now(), now())"
                ),
                {"id": product_id, "org": org_id},
            )
            conn.execute(
                text(
                    "INSERT INTO product_versions (id, organization_id, product_id,"
                    " version_no, label, status, created_at, updated_at)"
                    " VALUES (:id, :org, :p, 1, '', 'draft', now(), now())"
                ),
                {"id": version_id, "org": org_id, "p": product_id},
            )
            conn.execute(
                text(
                    "INSERT INTO analyses (id, organization_id, product_version_id, state,"
                    " idempotency_key, started_at, created_at, updated_at)"
                    " VALUES (:id, :org, :pv, 'queued', 'x', now(), now(), now())"
                ),
                {"id": analysis_id, "org": org_id, "pv": version_id},
            )
            conn.execute(
                text(
                    "INSERT INTO reports (id, organization_id, analysis_id, kind,"
                    " snapshot, sha256, generated_at, created_at, updated_at)"
                    " VALUES (:id, :org, :aid, 'json', '{}', :sha, now(), now(), now())"
                ),
                {"id": report_id, "org": org_id, "aid": analysis_id, "sha": "0" * 64},
            )
        for statement in (
            "UPDATE reports SET sha256 = repeat('1', 64)",
            "DELETE FROM reports",
        ):
            with pytest.raises(DatabaseError), pg_engine.begin() as conn:
                conn.execute(text(statement))

    def test_a_terminal_analysis_cannot_be_modified_but_a_live_one_can(self, pg_engine) -> None:
        """The conditional guarantee P5-T1 actually needs: `analyses` rows
        legitimately get UPDATEd many times before reaching a terminal
        state (once per transition), but never again afterward."""
        from sqlalchemy.exc import DatabaseError

        org_id, product_id, version_id, analysis_id = (uuid.uuid4() for _ in range(4))
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, retention_days,"
                    " cloud_ai_enabled, created_at, updated_at)"
                    " VALUES (:id, 'Org', 'org', 365, true, now(), now())"
                ),
                {"id": org_id},
            )
            conn.execute(
                text(
                    "INSERT INTO products (id, organization_id, name, internal_sku,"
                    " created_at, updated_at) VALUES (:id, :org, 'P', 'S1', now(), now())"
                ),
                {"id": product_id, "org": org_id},
            )
            conn.execute(
                text(
                    "INSERT INTO product_versions (id, organization_id, product_id,"
                    " version_no, label, status, created_at, updated_at)"
                    " VALUES (:id, :org, :p, 1, '', 'draft', now(), now())"
                ),
                {"id": version_id, "org": org_id, "p": product_id},
            )
            conn.execute(
                text(
                    "INSERT INTO analyses (id, organization_id, product_version_id, state,"
                    " idempotency_key, started_at, created_at, updated_at)"
                    " VALUES (:id, :org, :pv, 'queued', 'x', now(), now(), now())"
                ),
                {"id": analysis_id, "org": org_id, "pv": version_id},
            )

        # Non-terminal: an ordinary state-transition UPDATE succeeds.
        with pg_engine.begin() as conn:
            conn.execute(
                text("UPDATE analyses SET state = 'validating' WHERE id = :id"),
                {"id": analysis_id},
            )
            state = conn.execute(
                text("SELECT state FROM analyses WHERE id = :id"), {"id": analysis_id}
            ).scalar()
            assert state == "validating"

        # Reaching a terminal state, then any further UPDATE/DELETE is rejected.
        with pg_engine.begin() as conn:
            conn.execute(
                text("UPDATE analyses SET state = 'completed' WHERE id = :id"),
                {"id": analysis_id},
            )
        for statement in (
            "UPDATE analyses SET state = 'queued' WHERE id = :id",
            "DELETE FROM analyses WHERE id = :id",
        ):
            with pytest.raises(DatabaseError), pg_engine.begin() as conn:
                conn.execute(text(statement), {"id": analysis_id})
