#!/usr/bin/env bash
# Full 30d offline replay + import into live /data, then restart hermes-training.
set -euo pipefail
SSH_KEY="${BOT3_VPS_SSH_KEY:-$HOME/.ssh/bot3_cloud_agent}"
VPS_HOST="${BOT3_VPS_HOST:-207.246.96.45}"
SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "root@${VPS_HOST}")

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
"${SSH[@]}" "mkdir -p /opt/Bot-3/scripts /tmp/bot3-replay-src"
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -r \
  "$ROOT/scripts/polymarket-backfill" "root@${VPS_HOST}:/opt/Bot-3/scripts/"
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no \
  "$ROOT/hermes-agent-main/plugins/hermes-trading-engine/engine/pulse/offline_replay.py" \
  "root@${VPS_HOST}:/tmp/bot3-replay-src/offline_replay.py"
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no \
  "$ROOT/hermes-agent-main/plugins/hermes-trading-engine/engine/pulse/directional_cell_learning.py" \
  "root@${VPS_HOST}:/tmp/bot3-replay-src/directional_cell_learning.py"
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no \
  "$ROOT/hermes-agent-main/plugins/hermes-trading-engine/engine/pulse/engine.py" \
  "root@${VPS_HOST}:/tmp/bot3-replay-src/engine.py"

"${SSH[@]}" 'set -euo pipefail
PLUGIN=/opt/Bot-3/hermes-agent-main/plugins/hermes-trading-engine
# Patch engine modules into image layers used by next restart / one-off run.
docker cp /tmp/bot3-replay-src/offline_replay.py hermes-backfill:/app/engine/pulse/offline_replay.py
docker cp /tmp/bot3-replay-src/directional_cell_learning.py hermes-backfill:/app/engine/pulse/directional_cell_learning.py
docker cp /opt/Bot-3/scripts/polymarket-backfill/. hermes-backfill:/tmp/polymarket-backfill/

echo "Stopping hermes-training for ledger-safe import..."
docker stop hermes-training

echo "Running full offline replay on 30d data..."
docker exec hermes-backfill bash -lc "
  set -e
  mkdir -p /tmp/polymarket-backfill
  PYTHONPATH=/app python3 /tmp/polymarket-backfill/replay_offline.py \
    --data /data/polymarket-training --modes mid --holdout 0.30
  PYTHONPATH=/app python3 /tmp/polymarket-backfill/import_learner_priors.py \
    --replay /data/polymarket-training/replay --data-dir /data
  echo IMPORT_DONE
  cat /data/offline_import_manifest.json
  python3 - <<'\''PY'\''
import json
from pathlib import Path
rep = json.loads(Path(\"/data/polymarket-training/replay/walk_forward_report.json\").read_text())
print(\"HOLD_OUT_FAVORITES\", json.dumps(rep.get(\"holdout\", {}).get(\"favorites\"), indent=2))
print(\"HOLD_OUT_ALL\", json.dumps(rep.get(\"holdout\", {}).get(\"all\"), indent=2))
print(\"CELLS\", len(json.loads(Path(\"/data/directional_cell_learning.json\").read_text()).get(\"cells\") or {}))
ledger = json.loads(Path(\"/data/btc_pulse_ledger.json\").read_text())
print(\"LEDGER_CELLS\", len(((ledger.get(\"accounting_state\") or {}).get(\"cell_learning\") or {}).get(\"cells\") or {}))
PY
"

# Apply favorites env (stricter floor) then restart training with patched engine.
python3 /opt/Bot-3/scripts/setup-vps-favorites-ab-env.py
docker cp /tmp/bot3-replay-src/engine.py hermes-training:/app/engine/pulse/engine.py 2>/dev/null || true
docker cp /tmp/bot3-replay-src/directional_cell_learning.py hermes-training:/app/engine/pulse/directional_cell_learning.py 2>/dev/null || true
docker start hermes-training
# If start fails because we need recreate for env: use compose
cd "$PLUGIN"
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d hermes-training
sleep 12
docker ps --format "{{.Names}} {{.Status}}" | grep hermes
echo REPLAY_PIPELINE_OK
'