#!/usr/bin/env bash
# Bootstrap Bot 3 on a fresh VPS (run ON the VPS as root).
# Use when laptop git pull fails — clone directly on the server.
set -euo pipefail

VPS_REPO="${VPS_REPO:-/opt/Bot-3}"
GITHUB_REPO="${GITHUB_REPO:-https://github.com/minh99085/bot-3-clone-of-bot-1-.git}"
PLUGIN="$VPS_REPO/hermes-agent-main/plugins/hermes-trading-engine"

echo "==> Bot 3 VPS bootstrap -> $VPS_REPO"

if ! command -v git >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq git curl
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker..."
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
fi

mkdir -p "$VPS_REPO"
if [ ! -d "$VPS_REPO/.git" ]; then
  git clone "$GITHUB_REPO" "$VPS_REPO"
else
  cd "$VPS_REPO"
  git fetch origin main
  git reset --hard origin/main
  git clean -fd
fi

cd "$VPS_REPO"
echo "==> HEAD $(git rev-parse --short HEAD)"

SECRET_FILE="$PLUGIN/tradingview.secret"
if [ ! -f "$SECRET_FILE" ]; then
  cp "$PLUGIN/tradingview.secret.example" "$SECRET_FILE" 2>/dev/null || true
  echo ""
  echo "ACTION REQUIRED: edit $SECRET_FILE (paste TradingView secret on line 1)"
  echo "Also set XAI_API_KEY in $PLUGIN/.env before containers will pass validation."
  echo ""
fi

python3 scripts/setup-vps-training-env.py || true

cd "$PLUGIN"
docker compose down --remove-orphans 2>/dev/null || true
docker compose build
docker compose up -d --force-recreate --remove-orphans

sleep 8
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'hermes|NAMES' || docker ps

echo ""
echo "Dashboard : http://$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')/dashboard"
echo "TradingView webhook: http://$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')/webhooks/tradingview"
echo "Label: Bot 3 Directional"
