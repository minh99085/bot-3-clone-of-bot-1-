#!/usr/bin/env bash
# Run offline replay + import learner priors inside the VPS training container.
set -euo pipefail
SSH_KEY="${BOT3_VPS_SSH_KEY:-$HOME/.ssh/bot3_cloud_agent}"
VPS_HOST="${BOT3_VPS_HOST:-207.246.96.45}"
SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "root@${VPS_HOST}")

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
"${SSH[@]}" "mkdir -p /opt/Bot-3/scripts"
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -r \
  "$ROOT/scripts/polymarket-backfill" "root@${VPS_HOST}:/opt/Bot-3/scripts/"
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no \
  "$ROOT/hermes-agent-main/plugins/hermes-trading-engine/engine/pulse/offline_replay.py" \
  "root@${VPS_HOST}:/tmp/offline_replay.py"

"${SSH[@]}" 'docker cp /opt/Bot-3/scripts/polymarket-backfill hermes-training:/tmp/polymarket-backfill
docker cp /tmp/offline_replay.py hermes-training:/app/engine/pulse/offline_replay.py
docker exec hermes-training bash -lc "
  set -e
  PYTHONPATH=/app python3 /tmp/polymarket-backfill/replay_offline.py \
    --data /data/polymarket-training --modes mid --holdout 0.30
  PYTHONPATH=/app python3 /tmp/polymarket-backfill/import_learner_priors.py \
    --replay /data/polymarket-training/replay --data-dir /data
  echo IMPORT_DONE
  cat /data/offline_import_manifest.json
"'
