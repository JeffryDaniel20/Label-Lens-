#!/usr/bin/env bash
# Restore a pg_dump custom-format backup (P7-T6).
#
# Intended for a scratch/disaster-recovery environment, never a live
# production database without a deliberate, reviewed reason - `--clean
# --if-exists` will happily drop and recreate every object it finds.
#
# Usage: TARGET_DATABASE_URL=postgresql://user:pass@host:port/db \
#          ./restore_db.sh path/to/labellens-<timestamp>.dump
set -euo pipefail

: "${TARGET_DATABASE_URL:?TARGET_DATABASE_URL must be set (postgresql://user:pass@host:port/db)}"
dump_file="${1:?Usage: restore_db.sh <dump-file>}"

if [ ! -f "$dump_file" ]; then
  echo "No such backup file: $dump_file" >&2
  exit 1
fi

echo "Restoring $dump_file -> $TARGET_DATABASE_URL"
pg_restore --clean --if-exists --no-owner --no-acl --dbname="$TARGET_DATABASE_URL" "$dump_file"
echo "Restore complete."
