#!/usr/bin/env bash
# Bot 3 — one-shot local paper training via Docker Desktop.
# Run from repo root:  ./scripts/run-bot3-local-training.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN="$ROOT/hermes-agent-main/plugins/hermes-trading-engine"
PROJECT="bot3-local"
DASHBOARD_PORT=8810
COMPOSE=(-p "$PROJECT" -f docker-compose.yml -f docker-compose.local.yml)

cd "$ROOT"
echo "==> Preparing .env (Bot 3 local training)..."
python3 "$ROOT/scripts/setup-local-training-env.py"

cd "$PLUGIN"
echo "==> Stopping old containers..."
docker compose "${COMPOSE[@]}" down --remove-orphans

echo "==> Building images (RUN_TESTS=0 for local)..."
docker compose "${COMPOSE[@]}" build

echo "==> Starting hermes-training + hermes-trading-engine..."
docker compose "${COMPOSE[@]}" up -d --force-recreate --remove-orphans

echo ""
echo "Bot 3 local training is up."
echo "  Dashboard : http://127.0.0.1:${DASHBOARD_PORT}/dashboard"
echo "  Health    : http://127.0.0.1:${DASHBOARD_PORT}/api/health"
echo "  Logs      : docker compose -p $PROJECT -f docker-compose.yml -f docker-compose.local.yml logs -f hermes-training"
echo ""
sleep 8
if curl -sf "http://127.0.0.1:${DASHBOARD_PORT}/api/health" >/dev/null 2>&1; then
  curl -s "http://127.0.0.1:${DASHBOARD_PORT}/api/health"
  echo ""
else
  echo "Health check pending — training loop may still be warming up. Check logs above."
fi
