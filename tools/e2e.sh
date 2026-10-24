#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_FILE=docker-compose.yml:tools/compose.ci.yml
export COMPOSE_PROJECT_NAME="mosprom-ci-${GITHUB_RUN_ID:-$$}"
cleanup() {
  result=$?
  if (( result != 0 )); then docker compose logs --no-color --tail 100 api sim caddy || true; fi
  docker compose down -v --remove-orphans || true
  exit "$result"
}
trap cleanup EXIT
cat > .env <<'ENV'
APP_ENV=dev
DEV_AUTH=1
DOMAIN=http://localhost
WEBAPP_URL=http://localhost/
BOT_TOKEN=123456:TEST-BOT-TOKEN
INTERNAL_TOKEN=ci-only-internal-token-0123456789
ENV
export SIM_COMMIT
SIM_COMMIT="$(git rev-parse HEAD:vendor/frontend)"
docker compose up -d --build --wait --wait-timeout 300
export E2E_BASE E2E_DOCKER=1 E2E_BOT_TOKEN=123456:TEST-BOT-TOKEN
published="$(docker compose port caddy 80)"
E2E_BASE="http://localhost:${published##*:}"
python -m venv e2e/.venv
e2e/.venv/bin/pip install --require-hashes -r e2e/requirements.lock
(
  cd e2e/client
  npm ci
  npm audit --audit-level=high
  npm run typecheck
)
e2e/.venv/bin/python -m pytest e2e -q --junitxml=e2e/results.xml
e2e/.venv/bin/python tools/check_pytest.py e2e/results.xml
