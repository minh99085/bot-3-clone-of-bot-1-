#!/usr/bin/env bash
# Run one paper turn or overnight paper loop.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)"
export HERMES_LIVE=0

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -r requirements.txt
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

cmd="${1:-once}"
shift || true
python -m hermes.hermes_loop "$cmd" "$@"
