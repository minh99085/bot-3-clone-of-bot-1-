#!/usr/bin/env bash
# Sync origin/main -> Bot 3 VPS, then ALWAYS down --remove-orphans -> build -> up --remove-orphans.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

bash "$ROOT/scripts/materialize-vps-ssh-key.sh" 2>/dev/null || true
SSH_KEY="${BOT3_VPS_SSH_KEY:-$HOME/.ssh/hermes-laptop-vps}"
VPS_HOST="${BOT3_VPS_HOST:-207.246.96.45}"
VPS_USER="${BOT3_VPS_USER:-root}"
VPS_REPO="${BOT3_VPS_REPO:-/opt/Bot-3}"
PLUGIN_PATH="$VPS_REPO/hermes-agent-main/plugins/hermes-trading-engine"
GITHUB_REPO="${BOT3_GITHUB_REPO:-https://github.com/minh99085/bot-3-clone-of-bot-1-.git}"
SKIP_REBUILD="${SKIP_REBUILD:-0}"
VPS_SETUP_SCRIPT="${BOT3_VPS_SETUP_SCRIPT:-scripts/setup-vps-favorites-ab-env.py}"

SSH=(ssh -i "$SSH_KEY" -o ConnectTimeout=20 -o StrictHostKeyChecking=no "${VPS_USER}@${VPS_HOST}")
SCP=(scp -i "$SSH_KEY" -o StrictHostKeyChecking=no)

git fetch origin main
LOCAL="$(git rev-parse HEAD)"
ORIGIN="$(git rev-parse origin/main)"

if [[ "$LOCAL" != "$ORIGIN" ]]; then
  MB="$(git merge-base HEAD origin/main 2>/dev/null || true)"
  if [[ "$MB" == "$LOCAL" && "$LOCAL" != "$ORIGIN" ]]; then
    git pull --ff-only origin main
    LOCAL="$(git rev-parse HEAD)"
  fi
  if [[ "$LOCAL" != "$ORIGIN" ]]; then
    echo "ERROR: local HEAD ($LOCAL) != origin/main ($ORIGIN). Push or pull first." >&2
    exit 1
  fi
fi

ORIGIN="${ORIGIN,,}"
VPS_HEAD="$("${SSH[@]}" "git -C $VPS_REPO rev-parse HEAD 2>/dev/null || echo MISSING")"
VPS_HEAD="${VPS_HEAD,,}"

echo "BOT3 deploy -> ${VPS_USER}@${VPS_HOST}:$VPS_REPO"
echo "origin/main : ${ORIGIN:0:7} $ORIGIN"
echo "VPS HEAD    : ${VPS_HEAD:0:7} $VPS_HEAD"

remote_script() {
  local body="$1"
  local tmp="/tmp/grok-bot3-remote-$$.sh"
  printf '%s\n' "$body" | sed 's/\r$//' > "$tmp"
  "${SCP[@]}" "$tmp" "${VPS_USER}@${VPS_HOST}:/tmp/grok-bot3-remote.sh"
  "${SSH[@]}" "bash /tmp/grok-bot3-remote.sh; rm -f /tmp/grok-bot3-remote.sh"
  rm -f "$tmp"
}

if [[ ! "$VPS_HEAD" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Bootstrap VPS repo..."
  remote_script "$(cat <<EOF
set -e
sudo mkdir -p $VPS_REPO
sudo chown -R ${VPS_USER}:${VPS_USER} $VPS_REPO
if [ ! -d $VPS_REPO/.git ]; then
  git clone $GITHUB_REPO $VPS_REPO
fi
cd $VPS_REPO
git fetch origin main
git reset --hard origin/main
git clean -fd
echo VPS_HEAD=\$(git rev-parse HEAD)
EOF
)"
  VPS_HEAD="$("${SSH[@]}" "git -C $VPS_REPO rev-parse HEAD")"
  VPS_HEAD="${VPS_HEAD,,}"
fi

if [[ "$VPS_HEAD" != "$ORIGIN" ]]; then
  BUNDLE="$(mktemp /tmp/grok-bot3-sync.XXXXXX.bundle)"
  git bundle create "$BUNDLE" HEAD "^$VPS_HEAD"
  "${SCP[@]}" "$BUNDLE" "${VPS_USER}@${VPS_HOST}:/tmp/grok-bot3-sync.bundle"
  remote_script "$(cat <<EOF
set -e
cd $VPS_REPO
git fetch /tmp/grok-bot3-sync.bundle HEAD:refs/remotes/bundle/main
git reset --hard bundle/main
git clean -fd
rm -f /tmp/grok-bot3-sync.bundle
echo VPS_HEAD=\$(git rev-parse HEAD)
EOF
)"
  rm -f "$BUNDLE"
fi

if [[ "$SKIP_REBUILD" != "1" ]]; then
  remote_script "$(cat <<EOF
set -e
cd $VPS_REPO
python3 $VPS_SETUP_SCRIPT
python3 scripts/pulse-babysit/validate-frozen-lock.py $PLUGIN_PATH/.env || exit 1
cd $PLUGIN_PATH
docker compose -f docker-compose.yml -f docker-compose.vps.yml down --remove-orphans
docker compose -f docker-compose.yml -f docker-compose.vps.yml build
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --force-recreate --remove-orphans
bash $VPS_REPO/scripts/polymarket-backfill/install-vps-backfill-autostart.sh
sleep 8
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'hermes-training|hermes-trading-engine|hermes-backfill' || true
EOF
)"
fi

VPS_AFTER="$("${SSH[@]}" "git -C $VPS_REPO rev-parse HEAD")"
if [[ "$VPS_AFTER" != "$ORIGIN" ]]; then
  echo "SYNC FAIL: VPS=$VPS_AFTER origin=$ORIGIN" >&2
  exit 1
fi

echo "BOT3 SYNC OK - VPS HEAD matches origin/main (${ORIGIN:0:7})."
echo "Dashboard: http://${VPS_HOST}/dashboard"
echo "TradingView: http://${VPS_HOST}/webhooks/tradingview"
