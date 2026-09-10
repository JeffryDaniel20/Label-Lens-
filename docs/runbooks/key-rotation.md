# Runbook — secret and key rotation

Every secret in this codebase is a plain environment variable (`LABELLENS_*`), read once at process
start via `app.platform.config.Settings` - rotation is always "set the new value, restart the
process," never a database migration or code change. `app.platform.logging` already redacts
`secret_key` (and the equivalent provider/storage keys) from structured logs by field name, so a
rotated value is never visible in logs either before or after rotation.

## What can be rotated, and the blast radius of each

| Secret | Env var | Effect of rotating |
|---|---|---|
| App secret key | `LABELLENS_SECRET_KEY` | Reserved for future request-signing use; rotating it today has no session-invalidation side effect since sessions are opaque Redis-backed tokens, not signed by this key - confirm this is still true before relying on it if that changes. |
| LLM provider key | `LABELLENS_LLM_API_KEY` | Old key stops working the moment the provider revokes it; restart the API/worker processes with the new value first, *then* revoke the old key at the provider - not the other way round, or every in-flight `extracting` job fails. |
| Object storage keys | `LABELLENS_STORAGE_ACCESS_KEY`/`LABELLENS_STORAGE_SECRET_KEY` | Already-issued presigned URLs (uploads/downloads/report PDFs, all short-lived per `storage_upload_ttl_seconds`/`storage_download_ttl_seconds`) are signed with the *old* key and stop validating the instant it's revoked - acceptable since they expire in minutes anyway; don't rotate mid-upload of a large file. |
| Database password | part of `LABELLENS_DATABASE_URL` | Requires the DB user's password actually be changed first (`ALTER ROLE ... PASSWORD ...`), then the env var updated and every process (API, worker) restarted - a stale connection pool will keep working until it reconnects, so a full restart, not a rolling one, is the safe option here. |
| Sentry DSN | `LABELLENS_SENTRY_DSN` | A DSN authorizes *sending* events, not reading them - rotating it (create a new client key in the Sentry project, delete the old one) only requires an env var update and restart, no other side effect; `app.platform.sentry.init_sentry` reads it once at process start via the same cached `Settings`. |

## Procedure (any secret)

1. Generate the new value out-of-band (the provider's own dashboard, `openssl rand -hex 32` for
   `LABELLENS_SECRET_KEY`, etc.) - never derive it from, or log it alongside, the old one.
2. Update the host `.env` (600 permissions, never committed - section 34's own standing rule) or the
   Docker secret.
3. Restart the API and worker processes so `get_settings()` (which caches via `lru_cache`) picks up
   the new value - a bare env var change with no restart does nothing, since `Settings` is only ever
   constructed once per process.
4. Verify: hit `/healthz`/`/readyz`, then a real authenticated request, before revoking the old
   credential at its source.
5. Revoke the old credential at its source (provider dashboard, IAM console, `ALTER ROLE`) only after
   step 4 passes - revoking first, in case the new value has a typo, turns a rotation into an outage.
6. Record the rotation (what, when, who) in the audit trail if it was operator-initiated through the
   app itself (API key revocation already does this via `app.audit`); a raw env-var/infra rotation
   like the ones above has no in-app audit row, so note it wherever the team tracks infra changes.

## Suspected compromise (not routine rotation)

Treat step 5 as step 1 instead: revoke immediately, accept the resulting outage window, then rotate
and restart. A compromised credential live for a few more minutes is worse than a few minutes of
downtime.
