#!/usr/bin/env bash
# Nightly PostgreSQL backup (P7-T6).
#
# Dumps the whole database in pg_dump's custom format (compressed, and the
# only format `pg_restore` can selectively replay from), timestamped so
# every run produces a new, distinct file rather than overwriting the last
# one, uploads it to the configured S3-compatible bucket when one is set,
# and prunes local copies older than RETENTION_DAYS. Continuous WAL
# archiving (the other half of "RPO <= 24h" - this script alone only gives
# whatever RPO the backup interval itself is, e.g. 24h for a nightly cron)
# is configured separately on the PostgreSQL server itself; see
# infra/docker-compose.yml's `postgres` service and
# docs/runbooks/backup-and-restore.md.
#
# Usage: DATABASE_URL=postgresql://user:pass@host:port/db ./backup_db.sh
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must be set (postgresql://user:pass@host:port/db)}"
: "${BACKUP_DIR:=./backups}"
: "${RETENTION_DAYS:=30}"

mkdir -p "$BACKUP_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump_file="$BACKUP_DIR/labellens-$timestamp.dump"

echo "Backing up $DATABASE_URL -> $dump_file"
pg_dump --format=custom --file="$dump_file" "$DATABASE_URL"

if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  endpoint_arg=()
  if [ -n "${BACKUP_S3_ENDPOINT_URL:-}" ]; then
    endpoint_arg=(--endpoint-url "$BACKUP_S3_ENDPOINT_URL")
  fi
  key="postgres/$(basename "$dump_file")"
  aws s3 cp "${endpoint_arg[@]}" "$dump_file" "s3://$BACKUP_S3_BUCKET/$key"
  echo "Uploaded to s3://$BACKUP_S3_BUCKET/$key"
  # 30-day retention on the object-storage copy is a bucket lifecycle rule,
  # not this script's job - see the runbook for the example policy.
fi

find "$BACKUP_DIR" -name 'labellens-*.dump' -mtime "+$RETENTION_DAYS" -print -delete

echo "Backup complete: $dump_file"
