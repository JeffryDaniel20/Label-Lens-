#!/usr/bin/env bash
# Roll back to the last known-good image tag (P7-T7).
#
# Called automatically by `deploy.sh` when a new deploy fails its health
# gate, and safe to call manually against a live incident too. Never runs a
# migration and never touches the database - only application containers
# move, which is only safe because every migration is expand/migrate/
# contract-disciplined (see `deploy.sh`'s own header comment for the full
# argument): IMPLEMENTATION.md section 25's own words, "rollback never
# requires a down-migration."
#
# Usage:
#   REGISTRY=ghcr.io/you IMAGE_TAG=<previous-good-sha> \
#   COMPOSE_FILE=infra/docker-compose.prod.yml \
#     infra/scripts/rollback.sh
#
# `deploy.sh` also accepts IMAGE_TAG being pre-set (it exports the previous
# tag itself before calling this script); a manual invocation must set it
# explicitly to the tag being rolled back to.
set -uo pipefail

: "${COMPOSE_FILE:?COMPOSE_FILE must be set (e.g. infra/docker-compose.prod.yml)}"
: "${REGISTRY:?REGISTRY must be set}"
: "${IMAGE_TAG:?IMAGE_TAG must be set (the previous good commit SHA to roll back to)}"
: "${APP_SERVICES:=api worker-default worker-ocr worker-llm caddy}"
: "${HEALTH_TIMEOUT_SECONDS:=60}"
: "${HEALTH_POLL_INTERVAL_SECONDS:=3}"

state_file="$(dirname "$COMPOSE_FILE")/.deploy_state"

export REGISTRY IMAGE_TAG

log() { echo "[rollback] $*"; }

log "rolling back to IMAGE_TAG=$IMAGE_TAG (no migration will run)"
recovered=false
# shellcheck disable=SC2086
if docker compose -f "$COMPOSE_FILE" up -d $APP_SERVICES; then
  log "polling /readyz for up to ${HEALTH_TIMEOUT_SECONDS}s to confirm recovery"
  elapsed=0
  while [ "$elapsed" -lt "$HEALTH_TIMEOUT_SECONDS" ]; do
    if docker compose -f "$COMPOSE_FILE" exec -T api \
        sh -c 'curl -fsS http://localhost:8000/readyz | grep -q "\"status\": *\"ready\""' \
        >/dev/null 2>&1; then
      recovered=true
      break
    fi
    sleep "$HEALTH_POLL_INTERVAL_SECONDS"
    elapsed=$((elapsed + HEALTH_POLL_INTERVAL_SECONDS))
  done
else
  log "app containers failed to start on the rollback target image"
fi

if [ "$recovered" = true ]; then
  log "rollback successful - IMAGE_TAG=$IMAGE_TAG is healthy and remains the last known-good tag"
  echo "LAST_GOOD_TAG=$IMAGE_TAG" > "$state_file"
  exit 0
fi

log "rollback FAILED to recover health within ${HEALTH_TIMEOUT_SECONDS}s - manual intervention required"
exit 1
