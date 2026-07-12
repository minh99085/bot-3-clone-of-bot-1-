#!/usr/bin/env bash
# Pull pulse artifacts from VPS into vps_full_reports/latest/ (Linux/cloud).
# Mirrors pull-vps-artifacts.ps1 — wipe latest/, API + docker volume, optional push.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$REPO_ROOT/vps_full_reports/latest"
SSH_KEY="${BOT1_VPS_SSH_KEY:-$HOME/.ssh/bot1_grok_temp}"
VPS_HOST="${BOT1_VPS_HOST:-144.202.122.120}"
VPS_USER="${BOT1_VPS_USER:-root}"
CONTAINER="${BOT1_VPS_CONTAINER:-hermes-training}"
SKIP_PUSH="${SKIP_PUSH:-0}"

SSH=(ssh -i "$SSH_KEY" -o ConnectTimeout=20 -o StrictHostKeyChecking=no "${VPS_USER}@${VPS_HOST}")

rm -rf "$DEST"
mkdir -p "$DEST"
echo "Cleared and recreated $DEST"

fetch_api() {
  local url="$1" out="$2"
  curl -fsSL --max-time 30 "$url" -o "$out"
  if grep -q '"available"[[:space:]]*:[[:space:]]*false' "$out" 2>/dev/null; then
    echo "API unavailable: $url" >&2
    return 1
  fi
}

copy_remote() {
  local remote="$1" local="$2" binary="${3:-0}"
  if [[ "$binary" == "1" ]]; then
    "${SSH[@]}" "docker exec $CONTAINER base64 -w0 $remote" | base64 -d > "$local"
  else
    "${SSH[@]}" "docker exec $CONTAINER cat $remote" > "$local"
  fi
}

BASE="http://${VPS_HOST}"
if ! fetch_api "$BASE/api/polymarket/training/btc_pulse" "$DEST/btc_pulse_status.json"; then
  echo "API status failed; falling back to docker volume"
  copy_remote /data/btc_pulse_status.json "$DEST/btc_pulse_status.json"
fi
if ! fetch_api "$BASE/api/polymarket/training/btc_pulse/ledger" "$DEST/btc_pulse_ledger.json"; then
  copy_remote /data/btc_pulse_ledger.json "$DEST/btc_pulse_ledger.json"
fi
echo "  ok btc_pulse_status.json + ledger"

VOLUME_REQUIRED=(
  FULL_REPORT.md
  btc_pulse_light_report.json
  btc_pulse_tradingview.json
  report.md
  report.docx
  btc_pulse_score_history.json
  btc_pulse_meta_bundle.json
  LESSONS.md
  STATE.md
  MANIFEST.txt
  validation_full.txt
  validation_light.txt
)
for f in "${VOLUME_REQUIRED[@]}"; do
  if [[ "$f" == "report.docx" ]]; then
    copy_remote "/data/$f" "$DEST/$f" 1
  else
    copy_remote "/data/$f" "$DEST/$f"
  fi
  echo "  ok $f"
done
copy_remote /data/REPORT_EPOCH.json "$DEST/REPORT_EPOCH.json" 2>/dev/null || echo "  skip REPORT_EPOCH.json"

for f in btc_pulse_status.json report.docx FULL_REPORT.md; do
  [[ -f "$DEST/$f" ]] || { echo "Pull failed: missing $f" >&2; exit 1; }
done
echo "Pulled artifacts -> $DEST"

python3 "$REPO_ROOT/scripts/pulse-babysit/apply-report-epoch.py" || true
python3 "$REPO_ROOT/scripts/pulse-babysit/write-cycle-summary.py" || true
python3 "$REPO_ROOT/scripts/pulse-babysit/record-timeline.py" --from-latest || true
python3 "$REPO_ROOT/scripts/pulse-babysit/grade-technical.py" || true

if [[ "$SKIP_PUSH" != "1" ]]; then
  bash "$REPO_ROOT/scripts/pulse-babysit/push-report-to-main.sh"
fi
