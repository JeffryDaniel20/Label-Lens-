#!/usr/bin/env bash
# Health-gated deploy with automatic rollback (P7-T7).
#
# Deliberately generic over which compose file it drives
# (`infra/docker-compose.prod.yml` for a real deploy,
# `infra/docker-compose.drill.yml` for the local rollback drill this task's
# own acceptance criterion needs proven for real) - the mechanism is
# identical either way, only the target differs.
#
# Order matters and encodes IMPLEMENTATION.md section 25's own discipline:
# migrations run and commit *before* any app container is swapped, so the
# schema is always at least as new as the code that's about to run against
# it, and - because every migration is expand/migrate/contract-disciplined
# (`scripts/check_migration_discipline.py`) - the *previous* app version
# (what `rollback.sh` redeploys) keeps working against that same
# now-migrated schema too. Rollback therefore never touches the database at
# all - only application containers move.
#
# Usage (via `bash`, not a bare path - see this file's own internal call to
# rollback.sh for why: a real checkout never has the execute bit set):
#   REGISTRY=ghcr.io/you IMAGE_TAG=<sha> \
#   COMPOSE_FILE=infra/docker-compose.prod.yml \
#     bash infra/scripts/deploy.sh
set -uo pipefail

: "${COMPOSE_FILE:?COMPOSE_FILE must be set (e.g. infra/docker-compose.prod.yml)}"
: "${REGISTRY:?REGISTRY must be set}"
: "${IMAGE_TAG:?IMAGE_TAG must be set (the commit SHA to deploy)}"
: "${APP_SERVICES:=api worker-default worker-ocr worker-llm caddy}"
: "${HEALTH_TIMEOUT_SECONDS:=60}"
: "${HEALTH_POLL_INTERVAL_SECONDS:=3}"

state_file="$(dirname "$COMPOSE_FILE")/.deploy_state"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export REGISTRY IMAGE_TAG

log() { echo "[deploy] $*"; }

previous_tag=""
if [ -f "$state_file" ]; then
  # shellcheck disable=SC1090
  source "$state_file"
  previous_tag="${LAST_GOOD_TAG:-}"
fi

log "deploying IMAGE_TAG=$IMAGE_TAG (previous good tag: ${previous_tag:-none})"

# Deliberately *not* `set -e` for this whole script: a failed migration or a
# container that never comes up must fall through to the exact same
# rollback path a failed health check does, not crash out of it. Every
# fallible step below therefore checks its own exit code explicitly.
ready=false

log "running migrations (expand step) before touching any app container"
if docker compose -f "$COMPOSE_FILE" run --rm migrate; then
  log "starting app containers on the new image: $APP_SERVICES"
  # shellcheck disable=SC2086
  if docker compose -f "$COMPOSE_FILE" up -d $APP_SERVICES; then
    log "polling /readyz for up to ${HEALTH_TIMEOUT_SECONDS}s"
    elapsed=0
    while [ "$elapsed" -lt "$HEALTH_TIMEOUT_SECONDS" ]; do
      # `-f` (fail on non-2xx) plus grepping the body: a 200 with
      # `"status": "degraded"` (a real dependency down) must not pass the
      # gate either, only a genuine `"status": "ready"` does.
      if docker compose -f "$COMPOSE_FILE" exec -T api \
          sh -c 'curl -fsS http://localhost:8000/readyz | grep -q "\"status\": *\"ready\""' \
          >/dev/null 2>&1; then
        ready=true
        break
      fi
      sleep "$HEALTH_POLL_INTERVAL_SECONDS"
      elapsed=$((elapsed + HEALTH_POLL_INTERVAL_SECONDS))
    done
  else
    log "app containers failed to start on the new image"
  fi
else
  log "migration failed - the new release never ran against the database at all"
fi

if [ "$ready" = true ]; then
  log "deploy healthy - IMAGE_TAG=$IMAGE_TAG is now the last known-good tag"
  echo "LAST_GOOD_TAG=$IMAGE_TAG" > "$state_file"
  exit 0
fi

log "deploy FAILED to become healthy within ${HEALTH_TIMEOUT_SECONDS}s"
if [ -z "$previous_tag" ]; then
  log "no previous good tag recorded - nothing to roll back to. Manual intervention required."
  exit 1
fi

log "auto-rolling back to IMAGE_TAG=$previous_tag"
# `bash "$script_dir/..."`, not a bare path: a real deployment target checks
# this file out from git with `core.fileMode` off (or simply without the
# executable bit ever having been committed - Windows checkouts never track
# it), so relying on the execute permission bit here would fail with
# "Permission denied" the moment this ever runs somewhere other than a dev
# machine that happened to `chmod +x` it by hand. Found in a
# deployment-readiness audit; fixed the same way everywhere this repo
# invokes one of its own shell scripts from another.
COMPOSE_FILE="$COMPOSE_FILE" IMAGE_TAG="$previous_tag" REGISTRY="$REGISTRY" \
  APP_SERVICES="$APP_SERVICES" HEALTH_TIMEOUT_SECONDS="$HEALTH_TIMEOUT_SECONDS" \
  HEALTH_POLL_INTERVAL_SECONDS="$HEALTH_POLL_INTERVAL_SECONDS" \
  bash "$script_dir/rollback.sh"
exit 1
