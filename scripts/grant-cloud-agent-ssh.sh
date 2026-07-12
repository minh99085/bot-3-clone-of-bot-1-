#!/usr/bin/env bash
# One-time: grant cloud agent SSH access to Bot 3 VPS (run from laptop).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH_KEY="${BOT3_VPS_SSH_KEY:-$HOME/.ssh/hermes-laptop-vps}"
VPS_HOST="${BOT3_VPS_HOST:-207.246.96.45}"
VPS_USER="${BOT3_VPS_USER:-root}"
PUB_FILE="$ROOT/scripts/keys/bot3-cloud-agent.pub"

test -f "$PUB_FILE" || { echo "Missing $PUB_FILE — git pull first" >&2; exit 1; }
test -f "$SSH_KEY" || { echo "Missing private key: $SSH_KEY" >&2; exit 1; }

PUB="$(tr -d '\r' < "$PUB_FILE")"
ssh -i "$SSH_KEY" -o ConnectTimeout=20 -o StrictHostKeyChecking=no "${VPS_USER}@${VPS_HOST}" \
  "grep -qF 'bot3-cloud-agent' ~/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; grep bot3-cloud-agent ~/.ssh/authorized_keys"

echo "Cloud agent SSH granted on $VPS_HOST"
