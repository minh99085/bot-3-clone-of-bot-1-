#!/bin/sh
# Resumable Polymarket 30d backfill — auto-restart on crash, idle when manifest exists.
# Runs inside hermes-backfill container (docker-compose.vps.yml).
set -eu

OUTPUT="${BACKFILL_OUTPUT:-/data/polymarket-training}"
DAYS="${BACKFILL_DAYS:-30}"
LOG="${BACKFILL_LOG:-/data/polymarket-backfill.log}"
MANIFEST="${OUTPUT}/manifest.json"
INTERVAL="${BACKFILL_WATCH_INTERVAL:-45}"
RETRY_S="${BACKFILL_RETRY_S:-30}"
SCRIPT="${BACKFILL_SCRIPT:-/backfill/download_crypto_windows.py}"

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [backfill-watchdog] $*"
}

is_running() {
  for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$' || true); do
    cmd=$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *download_crypto_windows.py*) return 0 ;;
    esac
  done
  return 1
}

progress_line() {
  if [ -f "${OUTPUT}/checkpoint.json" ]; then
    python3 -c "
import json
from pathlib import Path
ck=json.loads(Path('${OUTPUT}/checkpoint.json').read_text())
s=ck.get('stats') or {}
print('windows=%s trades=%s errors=%s' % (s.get('windows'), s.get('trades'), s.get('errors')))
" 2>/dev/null || true
  fi
}

log "watchdog start output=${OUTPUT} days=${DAYS}"

while true; do
  if [ -f "$MANIFEST" ]; then
    log "complete — manifest present; sleeping 1h ($(progress_line))"
    sleep 3600
    continue
  fi

  if is_running; then
    sleep "$INTERVAL"
    continue
  fi

  if [ ! -f "$SCRIPT" ]; then
    log "ERROR missing script ${SCRIPT}"
    sleep 300
    continue
  fi

  log "starting download ($(progress_line))"
  set +e
  PYTHONPATH=/app python3 "$SCRIPT" \
    --days "$DAYS" --with-trades --output "$OUTPUT" \
    >>"$LOG" 2>&1
  rc=$?
  set -e
  log "download exited rc=${rc} ($(progress_line))"
  sleep "$RETRY_S"
done
