#!/usr/bin/env bash
# Install VPS host cron so Polymarket backfill survives reboots and compose deploys.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENSURE="${ROOT}/scripts/polymarket-backfill/ensure-backfill-running.sh"
CRON_LINE="*/10 * * * * BOT3_VPS_REPO=${BOT3_VPS_REPO:-/opt/Bot-3} ${ENSURE} >/dev/null 2>&1"

chmod +x "$ENSURE" "${ROOT}/scripts/polymarket-backfill/backfill-watchdog.sh"

if crontab -l 2>/dev/null | grep -qF "ensure-backfill-running.sh"; then
  echo "Cron already installed for backfill ensure"
else
  (crontab -l 2>/dev/null || true; echo "$CRON_LINE") | crontab -
  echo "Installed cron: $CRON_LINE"
fi

# Stop any legacy one-off backfill inside hermes-training (replaced by hermes-backfill).
docker exec hermes-training sh -c 'for pid in $(ls /proc 2>/dev/null | grep -E "^[0-9]+$"); do
  cmd=$(tr "\0" " " < /proc/$pid/cmdline 2>/dev/null || true)
  echo "$cmd" | grep -q download_crypto_windows.py && kill "$pid" 2>/dev/null || true
done' 2>/dev/null || true

# Start backfill service now if compose is available
PLUGIN="${BOT3_PLUGIN_PATH:-${BOT3_VPS_REPO:-/opt/Bot-3}/hermes-agent-main/plugins/hermes-trading-engine}"
if [ -d "$PLUGIN" ]; then
  cd "$PLUGIN"
  docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d hermes-backfill
  docker ps --format '{{.Names}} {{.Status}}' | grep hermes-backfill || true
fi
