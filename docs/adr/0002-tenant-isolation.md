# ADR 0002 — Two-layer tenant isolation

**Status:** accepted · 2026-08-29

## Context
A cross-tenant leak in a compliance product is fatal. Application-level filtering alone fails open
whenever a developer forgets a `WHERE organization_id = ...`.

## Decision
Two independent layers:
1. **Application scoping** — every tenant query goes through `tenant_scoped()`, which refuses models
   that have no `organization_id` column.
2. **PostgreSQL row-level security** — policies on every tenant table compare `organization_id`
   against the `app.org_id` session variable set per request in `set_tenant_context()`.

Cross-tenant identifiers return **404, never 403**, so an outsider cannot confirm that a resource
exists.

## Consequences
- SQLite (used by the fast test suite) has no RLS, so layer 2 is exercised by tests marked
  `postgres` in CI against a real PostgreSQL service.
- A `tests/security/` suite is a required CI gate; it asserts isolation on every tenant endpoint.

## Addendum — the application's database role must not be a superuser

Verified 2026-08-29 against a live PostgreSQL 16 container: **a superuser bypasses row-level
security unconditionally, even on a table declared with `FORCE ROW LEVEL SECURITY`.** The official
`postgres` Docker image's bootstrap user (created from `POSTGRES_USER`) is a superuser, so a naive
RLS test — or a naive production deployment — that connects as that bootstrap user will appear to
enforce isolation while actually not enforcing anything at all.

`tests/integration/test_migrations_and_rls.py` now provisions a throwaway, unprivileged
(`NOSUPERUSER`) role for the isolation assertion, and carries a second test
(`test_superuser_bypasses_rls_a_known_postgres_limitation`) that documents the bypass directly so a
future reader does not have to rediscover it.

**Action required before production deployment (tracked as part of P7-T7):** the API and worker
processes must authenticate as a dedicated, non-superuser role with only the privileges the schema
requires (`SELECT`/`INSERT`/`UPDATE`/`DELETE` on tenant tables, no `BYPASSRLS`). Migrations may
still run as a privileged owner role. This is a deployment/provisioning step, not a schema change,
and is deliberately out of scope for the Docker Compose *local development* stack, which optimizes
for a single one-command bring-up.
