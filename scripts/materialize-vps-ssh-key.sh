#!/usr/bin/env bash
# Materialize Bot 3 VPS SSH private key for cloud agents / CI.
# Reads PEM from BOT3_VPS_SSH_PRIVATE_KEY (preferred) or BOT3_VPS_SSH_KEY if value looks like PEM.
set -euo pipefail

KEY_PATH="${BOT3_VPS_SSH_KEY_PATH:-$HOME/.ssh/hermes-laptop-vps}"
CLOUD_KEY_PATH="${BOT3_CLOUD_SSH_KEY_PATH:-$HOME/.ssh/bot3_cloud_agent}"
mkdir -p "$(dirname "$KEY_PATH")"
chmod 700 "$(dirname "$KEY_PATH")"

_write_key() {
  local dest="$1"
  local pem="$2"
  printf '%s\n' "$pem" > "$dest"
  chmod 600 "$dest"
  echo "Wrote SSH key -> $dest"
}

if [[ -n "${BOT3_VPS_SSH_PRIVATE_KEY:-}" ]]; then
  _write_key "$KEY_PATH" "$BOT3_VPS_SSH_PRIVATE_KEY"
  export BOT3_VPS_SSH_KEY="$KEY_PATH"
  exit 0
fi

if [[ -n "${BOT3_VPS_SSH_KEY:-}" ]] && [[ "$BOT3_VPS_SSH_KEY" == *"BEGIN"* ]]; then
  _write_key "$KEY_PATH" "$BOT3_VPS_SSH_KEY"
  export BOT3_VPS_SSH_KEY="$KEY_PATH"
  exit 0
fi

if [[ -f "$KEY_PATH" ]]; then
  chmod 600 "$KEY_PATH" 2>/dev/null || true
  export BOT3_VPS_SSH_KEY="$KEY_PATH"
  echo "Using existing key: $KEY_PATH"
  exit 0
fi

if [[ -f "$CLOUD_KEY_PATH" ]]; then
  chmod 600 "$CLOUD_KEY_PATH" 2>/dev/null || true
  export BOT3_VPS_SSH_KEY="$CLOUD_KEY_PATH"
  echo "Using cloud agent key: $CLOUD_KEY_PATH"
  exit 0
fi

echo "No VPS SSH key available." >&2
echo "Set Cursor secret BOT3_VPS_SSH_PRIVATE_KEY (laptop private key PEM) or run grant-cloud-agent-ssh from laptop." >&2
exit 1
