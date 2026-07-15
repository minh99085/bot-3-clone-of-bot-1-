#!/usr/bin/env bash
# Run this ON the VPS (root console or existing SSH session) to install Hermes Paper.
# Dashboard: http://207.246.96.45/dashboard
set -euo pipefail

REPO_URL="${HERMES_REPO_URL:-https://github.com/minh99085/bot-3.git}"
REMOTE_PATH="${VPS_PATH:-/opt/financial-freedom-bot}"
BRANCH="${HERMES_BRANCH:-main}"

echo "=== Hermes v2 Paper bootstrap ==="
echo "Path: ${REMOTE_PATH}"

if ! command -v git >/dev/null 2>&1; then
  apt-get update && apt-get install -y git curl ca-certificates
fi

if [[ -d "${REMOTE_PATH}/.git" ]]; then
  echo "Updating existing checkout..."
  cd "${REMOTE_PATH}"
  git fetch origin "${BRANCH}"
  git checkout "${BRANCH}"
  git pull origin "${BRANCH}"
else
  echo "Cloning repository..."
  rm -rf "${REMOTE_PATH}"
  git clone --branch "${BRANCH}" --depth 1 "${REPO_URL}" "${REMOTE_PATH}"
  cd "${REMOTE_PATH}"
fi

# Env
if [[ ! -f .env ]]; then
  cp .env.example .env
fi
sed -i 's/^HERMES_PAPER_ONLY=.*/HERMES_PAPER_ONLY=1/' .env || true
sed -i 's/^HERMES_LIVE=.*/HERMES_LIVE=0/' .env || true
grep -q '^HERMES_PAPER_ONLY=' .env || echo 'HERMES_PAPER_ONLY=1' >> .env
grep -q '^HERMES_LIVE=' .env || echo 'HERMES_LIVE=0' >> .env
grep -q '^HERMES_CAPITAL=' .env || echo 'HERMES_CAPITAL=2000' >> .env
grep -q '^HERMES_HTTP_PORT=' .env || echo 'HERMES_HTTP_PORT=80' >> .env

mkdir -p data/paper data/live data/handoff logs knowledge
touch data/paper/.gitkeep logs/.gitkeep

# Docker
if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

# Firewall: HTTP + SSH only (8501 stays closed)
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH || true
  ufw allow 80/tcp || true
  ufw --force enable || true
fi

# systemd
cp deploy/hermes-paper.service /etc/systemd/system/hermes-paper.service
systemctl daemon-reload
systemctl enable hermes-paper.service
systemctl restart hermes-paper.service

echo "Waiting for containers..."
sleep 15
if docker compose version >/dev/null 2>&1; then
  docker compose ps
else
  docker-compose ps
fi

IP="$(curl -fsS ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
echo ""
echo "=== Deploy complete ==="
echo "Dashboard: http://${IP}/dashboard"
echo "Health:    http://${IP}/healthz"
echo "Logs:      docker compose -f ${REMOTE_PATH}/docker-compose.yml logs -f"
