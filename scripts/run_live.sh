#!/usr/bin/env bash
# Live mode — HARD GATED. Requires explicit flags + STATE.md live_enabled.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)"

if [[ "${HERMES_LIVE:-0}" != "1" ]]; then
  echo "Refusing live: set HERMES_LIVE=1 after paper WR>=80% evidence."
  exit 1
fi

if ! grep -qiE '\*\*Live Enabled\*\*: true' knowledge/STATE.md; then
  echo "Refusing live: set 'Live Enabled: true' in knowledge/STATE.md"
  exit 1
fi

# shellcheck disable=SC1091
[[ -f .venv/bin/activate ]] && source .venv/bin/activate
python -m hermes.hermes_loop once --live "$@"
