# LabelLens

AI-assisted, rules-decided compliance intelligence for product packaging.

**Core principle: AI extracts, rules decide.** Models produce structured, schema-validated,
evidence-cited facts. A deterministic, versioned rule engine makes every compliance call.

- [IMPLEMENTATION.md](IMPLEMENTATION.md) — the full architecture and task-level roadmap.
- [TESTTEST.md](TESTTEST.md) — live build status per task, with evidence.
- [docs/adr/](docs/adr/) — architecture decision records.
- [docs/runbooks/](docs/runbooks/) — operational runbooks (backup/restore, deploy/rollback,
  key rotation, provider outage, local development).

## Status

Every phase (0 through 7 — foundation, identity/tenancy, catalog/ingestion, the AI extraction and
rule-engine pipeline, analysis orchestration, the review frontend, and reporting/eval/hardening) is
implemented and tested. Two items remain, both genuinely outside what this repository can supply on
its own: D-03's cloud OCR vendor call needs real Google Cloud Vision credentials to exercise live,
and D-05 (production hosting target) is a real business decision — the deploy pipeline itself
(`infra/docker-compose.prod.yml`, `infra/scripts/deploy.sh`/`rollback.sh`,
`.github/workflows/deploy.yml`) is built and its rollback mechanism proven via a real local drill
(`make deploy-drill`), but has never run against an actual production host. See TESTTEST.md for the
exact, evidenced state of every task.

## Running it locally

```bash
make install          # create backend/.venv, install backend + frontend dependencies
make check            # ruff + mypy + the full backend test suite + frontend lint/typecheck/test
make up               # docker compose: api, worker (x3 queues), postgres, redis, minio
```

The frontend dev server (`cd frontend && npm run dev`) expects the API at `http://localhost:8000`,
which `make up` provides.

Without Docker, the backend test suite runs entirely on SQLite and needs no services:

```bash
cd backend && .venv/Scripts/python -m pytest -q     # Windows
cd backend && .venv/bin/python -m pytest -q         # macOS / Linux
```

PostgreSQL-only guarantees (row-level security, the append-only audit trigger), and every other
optional-service-gated suite (`object_storage`, `clamav`, `redis`, `llm`, `google_vision`,
`weasyprint`, `paddleocr`), are covered by their own pytest marker and skip automatically unless the
matching credential/service environment variable is set — see `backend/pyproject.toml`'s own marker
list for exactly which one each needs.

## Configuration

All settings come from `LABELLENS_*` environment variables; see [backend/.env.example](backend/.env.example).
Required secrets have no defaults — the process refuses to start without them.
