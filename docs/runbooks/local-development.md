# Runbook — local development

## First run
```bash
make install
cp backend/.env.example backend/.env   # then set LABELLENS_SECRET_KEY
make check
```

## Services
`make up` starts PostgreSQL, Redis, MinIO and the API with migrations applied.
`make down` removes them along with their volumes.

## Tests
- `make test` — full suite on SQLite, no services required.
- `pytest -m security` — tenant isolation, authorization, CSRF, enumeration.
- `pytest -m postgres` — RLS and the append-only trigger; requires `LABELLENS_TEST_PG_URL`.

## Migrations
```bash
cd backend && ../backend/.venv/Scripts/python -m alembic revision -m "description"
cd backend && ../backend/.venv/Scripts/python -m alembic upgrade head
```
Migrations must be reversible: `alembic downgrade base` is asserted by the test suite.

## Common problems
- **"Database engine is not initialised"** — `init_engine()` runs in `create_app()`; scripts that
  bypass the app factory must call it themselves.
- **Redis unavailable locally** — the app falls back to in-memory sessions and logs
  `redis_unavailable_using_memory`. In production this is an error, not a fallback.
