#!/usr/bin/env bash
# Deploy Financial Freedom Bot to VPS at /opt/financial-freedom-bot
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${VPS_HOST:-207.246.96.45}"
USER="${VPS_USER:-root}"
REMOTE_PATH="${VPS_PATH:-/opt/financial-freedom-bot}"
KEY="${VPS_SSH_KEY:-$HOME/.ssh/bot3_cloud_agent}"

SSH=(ssh -o StrictHostKeyChecking=accept-new)
RSYNC=(rsync -az --delete)
if [[ -f "$KEY" ]]; then
  SSH+=( -i "$KEY" )
  RSYNC+=( -e "ssh -i $KEY -o StrictHostKeyChecking=accept-new" )
fi

echo "Deploying to ${USER}@${HOST}:${REMOTE_PATH}"
"${SSH[@]}" "${USER}@${HOST}" "mkdir -p ${REMOTE_PATH}"
"${RSYNC[@]}" \
  --exclude '.venv' \
  --exclude '.git' \
  --exclude '.worktrees' \
  --exclude 'data/paper/*' \
  --exclude 'data/live/*' \
  --exclude '__pycache__' \
  "$ROOT/" "${USER}@${HOST}:${REMOTE_PATH}/"

"${SSH[@]}" "${USER}@${HOST}" "bash -s" <<EOF
set -euo pipefail
cd ${REMOTE_PATH}
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data/paper data/live data/handoff logs
echo "Deploy OK. Start paper overnight with:"
echo "  cd ${REMOTE_PATH} && source .venv/bin/activate && PYTHONPATH=. nohup python -m hermes.hermes_loop overnight --interval 300 > logs/hermes.log 2>&1 &"
EOF
