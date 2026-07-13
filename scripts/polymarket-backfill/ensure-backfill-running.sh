#!/usr/bin/env bash
# Host-side safety net: keep hermes-backfill container up (cron every 10 min on VPS).
set -euo pipefail

VPS_REPO="${BOT3_VPS_REPO:-/opt/Bot-3}"
PLUGIN="${BOT3_PLUGIN_PATH:-${VPS_REPO}/hermes-agent-main/plugins/hermes-trading-engine}"
LOG="${BOT3_BACKFILL_ENSURE_LOG:-/var/log/bot3-backfill-ensure.log}"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

if [ ! -d "$PLUGIN" ]; then
  echo "$(ts) skip: plugin path missing ($PLUGIN)" >>"$LOG"
  exit 0
fi

cd "$PLUGIN"

if ! docker compose -f docker-compose.yml -f docker-compose.vps.yml ps -q hermes-backfill 2>/dev/null | grep -q .; then
  echo "$(ts) starting hermes-backfill service" >>"$LOG"
  docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d hermes-backfill >>"$LOG" 2>&1 || true
else
  status=$(docker inspect -f '{{.State.Status}}' hermes-backfill 2>/dev/null || echo missing)
  if [ "$status" != "running" ]; then
    echo "$(ts) hermes-backfill status=${status}; restarting" >>"$LOG"
    docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d hermes-backfill >>"$LOG" 2>&1 || true
  fi
fi
