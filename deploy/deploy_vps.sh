#!/usr/bin/env bash
# Deploy Hermes v2 Paper Docker stack to VPS
# Dashboard: http://<VPS_IP>/dashboard
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${VPS_HOST:-207.246.96.45}"
USER="${VPS_USER:-root}"
REMOTE_PATH="${VPS_PATH:-/opt/financial-freedom-bot}"
KEY="${VPS_SSH_KEY:-$HOME/.ssh/bot3_cloud_agent}"

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

# Write key from Cursor secret if provided
if [[ -n "${BOT3_VPS_SSH_PRIVATE_KEY:-}" ]]; then
  printf '%s\n' "$BOT3_VPS_SSH_PRIVATE_KEY" > "$KEY"
  chmod 600 "$KEY"
  echo "Using SSH key from BOT3_VPS_SSH_PRIVATE_KEY"
fi

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
RSYNC=(rsync -az --delete)
if [[ -f "$KEY" ]]; then
  SSH+=( -i "$KEY" )
  RSYNC+=( -e "ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new" )
fi

echo "Testing SSH to ${USER}@${HOST}..."
if ! "${SSH[@]}" "${USER}@${HOST}" "echo ok" 2>/dev/null; then
  echo ""
  echo "ERROR: Cannot SSH to ${USER}@${HOST}"
  echo ""
  echo "Fix one of the following, then re-run ./deploy/deploy_vps.sh:"
  echo ""
  echo "  A) Add Cursor secret BOT3_VPS_SSH_PRIVATE_KEY (private key for root@${HOST})"
  echo "     Cursor → Cloud Agents → Environments → bot-3 → Secrets"
  echo ""
  echo "  B) Add this cloud-agent public key to VPS /root/.ssh/authorized_keys:"
  if [[ -f "${KEY}.pub" ]]; then
    cat "${KEY}.pub"
  elif [[ -f "$KEY" ]]; then
    ssh-keygen -y -f "$KEY" 2>/dev/null || true
  fi
  echo ""
  echo "  C) Run bootstrap on the VPS console (no SSH from here needed):"
  echo "     curl -fsSL https://raw.githubusercontent.com/minh99085/bot-3/main/deploy/bootstrap_on_vps.sh | bash"
  echo ""
  exit 1
fi

echo "Deploying Hermes Paper to ${USER}@${HOST}:${REMOTE_PATH}"
"${SSH[@]}" "${USER}@${HOST}" "mkdir -p ${REMOTE_PATH}"

"${RSYNC[@]}" \
  --exclude '.venv' \
  --exclude '.git' \
  --exclude '.worktrees' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude 'data/paper/*' \
  --exclude 'data/live/*' \
  --exclude 'data/handoff/*' \
  --exclude 'logs/*' \
  --exclude '.env' \
  "$ROOT/" "${USER}@${HOST}:${REMOTE_PATH}/"

"${SSH[@]}" "${USER}@${HOST}" "bash -s" <<EOF
set -euo pipefail
cd ${REMOTE_PATH}

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi
sed -i 's/^HERMES_PAPER_ONLY=.*/HERMES_PAPER_ONLY=1/' .env || true
sed -i 's/^HERMES_LIVE=.*/HERMES_LIVE=0/' .env || true
grep -q '^HERMES_PAPER_ONLY=' .env || echo 'HERMES_PAPER_ONLY=1' >> .env
grep -q '^HERMES_LIVE=' .env || echo 'HERMES_LIVE=0' >> .env
grep -q '^HERMES_CAPITAL=' .env || echo 'HERMES_CAPITAL=2000' >> .env

mkdir -p data/paper data/live data/handoff logs knowledge
touch data/paper/.gitkeep logs/.gitkeep

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH || true
  ufw allow 80/tcp || true
  ufw --force enable || true
  echo "UFW: SSH + 80 allowed; 8501 stays closed"
fi

cp deploy/hermes-paper.service /etc/systemd/system/hermes-paper.service
systemctl daemon-reload
systemctl enable hermes-paper.service
systemctl restart hermes-paper.service

sleep 12
docker compose ps 2>/dev/null || docker-compose ps
curl -fsS http://127.0.0.1/healthz && echo " nginx ok" || echo "WARN: nginx health pending"
EOF

echo ""
echo "=== Deployed ==="
echo "Dashboard: http://${HOST}/dashboard"
echo "Health:    http://${HOST}/healthz"
echo "SSH logs:  ssh ${USER}@${HOST} 'docker compose -f ${REMOTE_PATH}/docker-compose.yml logs -f'"
