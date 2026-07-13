#!/usr/bin/env bash
# Pull completed Polymarket training data from Bot 3 VPS to your Samsung T7.
# Run on your Windows laptop (Git Bash or WSL) after the backfill finishes.
set -euo pipefail

VPS_HOST="${BOT3_VPS_HOST:-207.246.96.45}"
VPS_USER="${BOT3_VPS_USER:-root}"
SSH_KEY="${BOT3_VPS_SSH_KEY:-$HOME/.ssh/bot3_cloud_agent}"
REMOTE_DIR="${BOT3_REMOTE_DATA:-/opt/Bot-3/data/polymarket-training}"
LOCAL_DIR="${1:-D:/polymarket-training}"

SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "${VPS_USER}@${VPS_HOST}")
SCP=(scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -r)

echo "Checking remote manifest..."
"${SSH[@]}" "test -f ${REMOTE_DIR}/manifest.json" || {
  echo "Remote data not ready yet. Wait for backfill to finish." >&2
  exit 1
}
"${SSH[@]}" "cat ${REMOTE_DIR}/manifest.json"

mkdir -p "$LOCAL_DIR"
echo "Downloading ${VPS_USER}@${VPS_HOST}:${REMOTE_DIR} -> ${LOCAL_DIR}"
"${SCP[@]}" "${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/" "$LOCAL_DIR/"
echo "Done. Training data is at: $LOCAL_DIR"
