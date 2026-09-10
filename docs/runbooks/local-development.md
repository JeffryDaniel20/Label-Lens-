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

## Observability

`GET /metrics` is always available. Prometheus, Grafana, and the alert rules live in an optional
overlay - see [observability.md](observability.md).

## Backups and restore

`make up`'s `postgres` service runs with continuous WAL archiving on by default (to a local named
volume in dev). See [backup-and-restore.md](backup-and-restore.md) for the nightly `pg_dump` script,
the restore script, and the quarterly drill log.

## Frontend

`frontend/` is a Vite + React + TypeScript app (see its own README for the usual `npm run dev`/
`build`/`test`/`lint` scripts). `npm run generate:api-types` regenerates `src/api/schema.ts` from
the backend's live OpenAPI schema (`backend/scripts/export_openapi.py`, no running server needed) -
re-run it whenever a backend endpoint's request/response shape changes. `npm run e2e` runs the real
Playwright browser suite, which starts both a real backend (migrated to head, against a disposable
`e2e_test.db`) and the Vite dev server itself - see `frontend/playwright.config.ts`. The catalog/
upload suite (`e2e/catalog.spec.ts`, P6-T2) additionally needs real MinIO reachable at
`localhost:9000` - run `docker compose -f infra/docker-compose.yml up -d minio minio-init` first;
its upload flow is a real presigned PUT straight from the browser to object storage, not a mock.

## Common problems
- **"Database engine is not initialised"** — `init_engine()` runs in `create_app()`; scripts that
  bypass the app factory must call it themselves.
- **Redis unavailable locally** — the app falls back to in-memory sessions and logs
  `redis_unavailable_using_memory`. In production this is an error, not a fallback.
- **`ImportError: DLL load failed while importing _pillow_heif: An Application Control policy has
  blocked this file`** — a machine-level Windows security policy blocking that specific venv's copy
  of `pillow_heif`'s compiled extension, not a code or dependency problem: reinstalling the package
  (even a different version, a different wheel, entirely different hash) does not clear it, which
  rules out a corrupted download. Found live while wiring up P6-T1's E2E suite (2026-09-09) - a
  sibling venv (`.venv312`, originally set up for PaddleOCR verification) imported the exact same
  code cleanly at the same moment, confirming this is scoped to the one venv's file path, not the
  package, the machine as a whole, or anything about this codebase. Whoever manages the machine's
  Application Control / WDAC policy needs to actually resolve this — it is not something `pip` or
  this repository can route around, and this file does not attempt to.
- **A Playwright `webServer` command exits instantly with no output ("Process from
  config.webServer exited early")** — if the command string is `(if exist X del X) && A && B`
  and `X` doesn't exist, this is almost certainly *not* that; but a similarly-shaped `if exist X
  del X && A && B` **without** the parentheses around the `if` is a real bug, not a fluke: cmd.exe's
  grammar extends an unparenthesized `IF condition command` to the end of the line, so the whole
  `del X && A && B` is treated as a single command gated on the `if`. The first time `X` doesn't
  exist (a fresh checkout, or right after a previous run's own cleanup), the entire line - `A` and
  `B` included - is silently skipped and cmd.exe exits having started nothing. It only reproduces on
  a genuinely fresh run, which is why it can look like it "used to work" - found live while wiring
  up P6-T2's upload E2E test (2026-09-09), see `frontend/playwright.config.ts`'s own comment on this.
