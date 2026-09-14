# Runbook — deploy and rollback (P7-T7)

## Architecture

Matches IMPLEMENTATION.md section 25 literally: `api`, `worker-default`/`worker-ocr`/`worker-llm`,
`postgres`, `redis`, `caddy` (TLS termination + HSTS, `infra/Caddyfile`). Images are built once in
CI (`.github/workflows/deploy.yml`), tagged by commit SHA, and pulled - never built - on the deploy
target (`infra/docker-compose.prod.yml`). Migrations run as a one-shot `migrate` container before
any app container is touched.

**Hosting target (D-05) is still an open decision** - single VPS vs Fly/Render - and is not resolved
here: resolving it means committing to a real host, domain, and billing relationship, which is a
business decision for whoever operates this deployment, not something to fabricate in this
repository. `infra/docker-compose.prod.yml` and the CI/CD pipeline below are written to be equally
usable under either choice (both are plain Docker Compose targets reachable over SSH); nothing about
them presumes one over the other.

## Expand/migrate/contract discipline

"Every migration must be backward-compatible with the previous app version... so rollback never
requires a down-migration" (section 25). `backend/scripts/check_migration_discipline.py` enforces
this statically and is a required CI check (`make migration-discipline`): every real migration in
`backend/migrations/versions/` is verified clean by `tests/unit/test_migration_discipline.py`'s own
`TestTheRealRepository` case, run directly against the real directory, not a copy.

This is *why* rollback is safe to never touch the database: the schema only ever moves forward, and
every forward step is something the previous app version already tolerates.

## The pipeline (`.github/workflows/deploy.yml`)

On push to `main`: build + push the image to GHCR → deploy to staging over SSH
(`infra/scripts/deploy.sh`) → smoke-test staging's public `/healthz`/`/readyz` → **await manual
approval** (a GitHub Environment protection rule on the `production` environment - a real platform
feature, configured by whoever administers the repository, not simulated) → deploy to production the
same way.

Every host, SSH key, and domain the workflow references is a GitHub Actions secret an operator must
configure (`STAGING_HOST`, `STAGING_SSH_USER`, `STAGING_SSH_KEY`, `STAGING_DOMAIN`,
`PRODUCTION_HOST`, `PRODUCTION_SSH_USER`, `PRODUCTION_SSH_KEY`) - none are fabricated in this
repository, and the workflow has never fired as a real GitHub Actions run with real
`STAGING_*`/`PRODUCTION_*` secrets and a real target host behind them - firing it for real needs a
chosen host per D-05, plus that host and every secret actually provisioned. What *has* been verified
for real is the mechanism the workflow drives on the target host - see the drill below.

## `deploy.sh` / `rollback.sh`

```
REGISTRY=ghcr.io/you IMAGE_TAG=<sha> COMPOSE_FILE=infra/docker-compose.prod.yml \
  bash infra/scripts/deploy.sh
```

Always invoked via `bash`, never as a bare path or `./deploy.sh` - a real `git clone`/`git pull`
checkout never carries the execute bit (this repository's own Windows-checked-out history never
recorded it either, `core.fileMode=false`), so relying on it would fail with "Permission denied" on
a real target the first time this ever ran outside a machine someone had manually `chmod +x`-ed it
on. A deployment-readiness audit found this same gap in three other places (`deploy.sh`'s own call
to `rollback.sh`, `deploy_drill.sh`'s calls to `deploy.sh`, and `backup_db.sh`/`restore_db.sh`'s own
usage comments) and fixed all of them the same way.

1. Runs `docker compose run --rm migrate` (the expand step) - if this fails, the release never
   touches a single app container and the rollback path below fires immediately (there is nothing
   to roll back to on a first deploy; every deploy after that rolls back to the tag recorded the
   last time this script succeeded, in `infra/.deploy_state`, gitignored - runtime state on the
   target host, never committed).
2. Starts/recreates `api`/`worker-*`/`caddy` on the new image.
3. Polls the `api` container's own `/readyz` (via `docker compose exec`, not through Caddy or DNS -
   the gate must reflect the container's own health, not network/TLS propagation) for up to
   `HEALTH_TIMEOUT_SECONDS` (default 60s).
4. **Healthy**: records the new tag as the last known-good one, exits 0.
   **Not healthy**: calls `rollback.sh` with the previous good tag automatically, exits 1.

`rollback.sh` never runs a migration - it only ever redeploys a previous image against whatever
schema is already live, which the expand/migrate/contract discipline above makes safe.

## The rollback drill (the task's own literal acceptance criterion)

> "a deliberately broken deploy auto-rolls back with no data loss"

Run for real, locally, via `make deploy-drill` (`infra/scripts/deploy_drill.sh`), against
`infra/docker-compose.drill.yml` - a real Postgres, Redis, and MinIO, a real migration run, and the
real application image's own `/readyz` deciding health. No stub anywhere in this loop.

1. Build the real application image (`local/labellens-api:good`) and a genuinely broken release
   (`local/labellens-api:broken` - `FROM ...:good`, `CMD` replaced with one that raises on startup,
   the same class of failure as a real code regression that crashes on boot).
2. Deploy `good` via the real `deploy.sh` - it becomes healthy and is recorded as the last good tag.
3. Seed real data through the real running API (a signup + a product).
4. Deploy `broken` via the same `deploy.sh` - it never becomes healthy (the container crashes on
   every start), so `deploy.sh` automatically calls `rollback.sh` with `good`.
5. Verify `/readyz` is healthy again, the state file records `good` again, and - **the "no data
   loss" half** - log back in with the seeded credentials and confirm the seeded product is still
   there via a real `GET /v1/products` call against the same, never-recreated database.

| Date | Operator | Result |
|---|---|---|
| 2026-09-14 | automated session (see TESTTEST.md P7-T7) | **Pass** - `deploy.sh` correctly reported failure for the broken release, `rollback.sh` restored `good` and confirmed `/readyz` healthy, and the seeded product ("Drill Chips", SKU `DRILL-001`) was read back byte-for-byte after rollback via a fresh login against the untouched database. |

Record every real drill here, dated - an undated or unexecuted line does not count as a drill (the
same standard `docs/runbooks/backup-and-restore.md`'s own restore drill log already holds itself to).

## What remains before a real deployment

- **D-05** (hosting target) must actually be decided by whoever operates this deployment.
- A real host, domain, and set of GitHub Actions secrets must be provisioned and configured - none
  of which this repository can supply on its own.
- `infra/Caddyfile`'s `{$DOMAIN}` needs a real domain this deployment owns for Automatic HTTPS to
  issue a certificate against.
