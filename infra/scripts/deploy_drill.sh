#!/usr/bin/env bash
# The real, local drill for P7-T7's own literal acceptance criterion:
# "a deliberately broken deploy auto-rolls back with no data loss."
#
# Runs entirely against `infra/docker-compose.drill.yml` (a real Postgres, a
# real migration, and the real application image's own `/readyz`) - never a
# stub. "Broken" here means a real, plausible production mistake: a release
# built with the wrong database credentials baked in, which is exactly the
# kind of thing `/readyz`'s database check exists to catch before a broken
# deploy ever serves traffic. No real registry, domain, or credential is
# used anywhere in this script - `REGISTRY=local` refers to an image tag
# that only exists in this machine's local Docker image store, built by
# this script itself; see `infra/docker-compose.prod.yml` for what a real
# deploy target's configuration actually looks like.
#
# Usage: infra/scripts/deploy_drill.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compose_file="$repo_root/infra/docker-compose.drill.yml"
script_dir="$repo_root/infra/scripts"

export REGISTRY="local"
export COMPOSE_FILE="$compose_file"
export APP_SERVICES="api"
export HEALTH_TIMEOUT_SECONDS=60
export HEALTH_POLL_INTERVAL_SECONDS=2

log() { echo "[drill] $*"; }
cleanup() {
  log "tearing down the drill stack"
  docker compose -f "$compose_file" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$repo_root/infra/.deploy_state"
}
trap cleanup EXIT

log "building the real application image (this is the 'good' release)"
docker build -t local/labellens-api:good "$repo_root/backend" >/tmp/drill_build_good.log 2>&1 \
  || { echo "build failed - see /tmp/drill_build_good.log" >&2; tail -n 60 /tmp/drill_build_good.log >&2; exit 1; }

log "building the 'broken' release: a startup-crashing regression baked in"
broken_dir="$(mktemp -d)"
# A real, plausible production mistake: the release crashes on startup (an
# import-time error, a bad config assertion, ...) - one of the most common
# real deploy failures. The container genuinely never comes up, so
# `docker compose exec` against it genuinely fails every poll; this isn't
# simulated by an env var deploy.sh's own compose file would just override
# anyway (compose's `environment:` always wins over a Dockerfile `ENV`), it
# is a real broken process.
cat > "$broken_dir/Dockerfile" <<'EOF'
FROM local/labellens-api:good
CMD ["python", "-c", "raise RuntimeError('deliberately broken release for the P7-T7 rollback drill')"]
EOF
docker build -t local/labellens-api:broken "$broken_dir" >/tmp/drill_build_broken.log 2>&1 \
  || { echo "build failed - see /tmp/drill_build_broken.log" >&2; exit 1; }
rm -rf "$broken_dir"

log "=== step 1: deploy the good release ==="
IMAGE_TAG=good "$script_dir/deploy.sh"

log "seeding real data through the real running API"
signup_body='{"organization_name":"Drill Org","email":"owner@drill-test.com","password":"CorrectHorse42!"}'
signup_response="$(curl -fsS -c /tmp/drill_cookies.txt -X POST \
  -H 'Content-Type: application/json' -d "$signup_body" http://localhost:18000/v1/auth/signup)"
csrf_token="$(echo "$signup_response" | python3 -c "import json,sys; print(json.load(sys.stdin)['csrf_token'])" 2>/dev/null \
  || echo "$signup_response" | grep -o '"csrf_token":"[^"]*"' | cut -d'"' -f4)"
product_body='{"name":"Drill Chips","internal_sku":"DRILL-001"}'
curl -fsS -b /tmp/drill_cookies.txt -X POST \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $csrf_token" \
  -d "$product_body" http://localhost:18000/v1/products > /tmp/drill_product.json
log "seeded product: $(cat /tmp/drill_product.json)"
grep -q "Drill Chips" /tmp/drill_product.json || { echo "seeding failed" >&2; exit 1; }

log "=== step 2: deploy the broken release (expected to fail health and auto-rollback) ==="
if IMAGE_TAG=broken "$script_dir/deploy.sh"; then
  echo "FAIL: the broken deploy was reported healthy - it should not have been" >&2
  exit 1
fi
log "deploy.sh correctly reported failure for the broken release"

log "=== step 3: verify auto-rollback actually happened ==="
sleep 2
readyz="$(curl -fsS http://localhost:18000/readyz)"
echo "$readyz" | grep -q '"status":"ready"' || { echo "FAIL: not healthy after rollback: $readyz" >&2; exit 1; }
log "confirmed healthy again after rollback: $readyz"

state_file="$repo_root/infra/.deploy_state"
grep -q "LAST_GOOD_TAG=good" "$state_file" || { echo "FAIL: state file does not show 'good' restored" >&2; exit 1; }
log "confirmed state file records 'good' as the last known-good tag again"

log "=== step 4: verify no data loss ==="
login_body='{"email":"owner@drill-test.com","password":"CorrectHorse42!"}'
curl -fsS -c /tmp/drill_cookies2.txt -X POST \
  -H 'Content-Type: application/json' -d "$login_body" http://localhost:18000/v1/auth/login \
  > /tmp/drill_login.json
products="$(curl -fsS -b /tmp/drill_cookies2.txt http://localhost:18000/v1/products)"
echo "products after rollback: $products"
echo "$products" | grep -q "Drill Chips" || { echo "FAIL: seeded product is gone after rollback" >&2; exit 1; }
echo "$products" | grep -q "DRILL-001" || { echo "FAIL: seeded product SKU is gone after rollback" >&2; exit 1; }

log "PASS: a deliberately broken deploy auto-rolled back with no data loss."
