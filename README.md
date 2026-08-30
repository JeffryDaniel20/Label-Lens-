# LabelLens

AI-assisted, rules-decided compliance intelligence for product packaging.

**Core principle: AI extracts, rules decide.** Models produce structured, schema-validated,
evidence-cited facts. A deterministic, versioned rule engine makes every compliance call.

- [IMPLEMENTATION.md](IMPLEMENTATION.md) — the full architecture and task-level roadmap.
- [TESTTEST.md](TESTTEST.md) — live build status per task, with evidence.
- [docs/adr/](docs/adr/) — architecture decision records.

## Status

Phases 0 and 1 are implemented and tested (foundation, identity, tenancy, RBAC, audit log), plus
the catalog slice of Phase 2. The AI pipeline, rule engine, analysis orchestration, frontend and
reporting are not built yet. See TESTTEST.md for the exact state of every task.

## Running it locally

```bash
make install          # create backend/.venv and install dependencies
make check            # ruff + mypy + the full test suite
make up               # docker compose: api, postgres, redis, minio
```

Without Docker, the test suite runs entirely on SQLite and needs no services:

```bash
cd backend && .venv/Scripts/python -m pytest -q     # Windows
cd backend && .venv/bin/python -m pytest -q         # macOS / Linux
```

PostgreSQL-only guarantees (row-level security, the append-only audit trigger) are covered by tests
marked `postgres`, which run when `LABELLENS_TEST_PG_URL` points at a live database.

## Configuration

All settings come from `LABELLENS_*` environment variables; see [backend/.env.example](backend/.env.example).
Required secrets have no defaults — the process refuses to start without them.
