# Runbook — backup and restore (P7-T6)

## What's backed up

The PostgreSQL database only. Object storage (uploaded files, page renders, report PDFs) relies on
the bucket's own versioning (enable it on the production bucket - `mc version enable
local/labellens-uploads` for MinIO, or the bucket's versioning setting on R2/S3) rather than a
second backup path, since it is already content-addressed and rarely deleted outside retention
purges (P7-T8).

## Two layers, two RPOs

1. **Nightly `pg_dump`** (`infra/scripts/backup_db.sh`) - a full logical dump in custom format,
   timestamped, uploaded to the configured bucket, with local copies past `RETENTION_DAYS` (default
   30) pruned. RPO: up to 24h (whatever's changed since the last nightly run).
2. **Continuous WAL archiving** - `infra/docker-compose.yml`'s `postgres` service runs with
   `archive_mode=on` and copies every completed WAL segment to `/wal_archive` (a named volume in
   dev; point `archive_command` at a bucket-synced path in production). RPO: minutes, not hours -
   this is what actually gets IMPLEMENTATION.md section 33's "RPO <= 24h" target down from "since
   last night" to "since the last segment," and is required for point-in-time recovery.

## Scheduling in production

Cron (or a systemd timer) on the VPS, nightly:
```
0 2 * * * DATABASE_URL=postgresql://... BACKUP_S3_BUCKET=labellens-backups \
  /srv/infra/scripts/backup_db.sh >> /var/log/labellens-backup.log 2>&1
```
An example bucket lifecycle rule for the 30-day retention mentioned in the objective (MinIO/S3 JSON):
```json
{"Rules": [{"ID": "expire-postgres-backups", "Filter": {"Prefix": "postgres/"},
            "Status": "Enabled", "Expiration": {"Days": 30}}]}
```

## Restoring

```
TARGET_DATABASE_URL=postgresql://user:pass@host:port/db \
  infra/scripts/restore_db.sh path/to/labellens-<timestamp>.dump
```
`--clean --if-exists` means this is safe to run against a database that already has the old schema
in it (a genuine disaster-recovery restore) as well as a truly empty one (a scratch drill) - existing
objects are dropped first, so the result matches the dump exactly either way.

**Never point `TARGET_DATABASE_URL` at a live production database as a drill target.** Drills run
against a disposable scratch instance (see below); a real production restore is an incident, not a
drill, and follows this same script with sign-off from whoever declared the incident.

## Restore drill (quarterly, per section 33 and the acceptance criterion)

Each drill: bring up a disposable scratch PostgreSQL container, take a real backup of a real
database with real data in it, restore that backup into the scratch instance, and verify the data
actually arrived - not just that the commands exited zero.

| Date | Operator | Backup source | Rows verified | Result |
|---|---|---|---|---|
| 2026-09-08 | automated session (see TESTTEST.md P7-T6) | a real signup + product created through the actual app (`POST /v1/auth/signup`, `POST /v1/products`) against a fresh `ll-pg-drill-source` container migrated to head with `backup_db.sh` run for real inside a `postgres:16-alpine` container | `organizations`/`users`/`products` row counts (1/1/1) and the org's exact name+slug, the user's exact email, and the product's exact name+SKU all matched byte-for-byte between source and the restored `ll-pg-drill-scratch` container via `restore_db.sh`; the app itself then logged in against the restored DB and read the same product back over `GET /v1/products` | **Pass** |

Record every real drill here, dated - an undated or unexecuted line in this table is not a drill.

## Common problems

- **`archive command failed ... Permission denied` in the postgres logs** - a freshly created named
  volume is root-owned; `archive_command` runs as the `postgres` user (uid 70 in this image) and
  cannot write to it until something chowns it first. `infra/docker-compose.yml`'s
  `postgres-wal-init` service does exactly that, once, before `postgres` starts (`depends_on:
  postgres-wal-init: {condition: service_completed_successfully}`) - the same pattern `minio-init`
  already uses for its own one-time bucket setup. Found and fixed live while running the drill below:
  WAL archiving silently failed on every segment until this init step was added.
- **`pg_dump: error: server version mismatch`** - the client tools inside the backup script's own
  container/host must be the same major version as the server (16.x here); run `backup_db.sh` from
  something built on the same `postgres:16-alpine` base, not an arbitrary client install.
- **Restore succeeds but the app fails to start** - a dump taken from an older schema version needs
  `alembic upgrade head` run against it afterward; the dump captures data and schema as of when it
  was taken, not "whatever migration state the app currently expects."
