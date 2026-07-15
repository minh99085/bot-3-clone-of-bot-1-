#!/usr/bin/env bash
# Reset 5-instance paper fleet to cold start ($2k × 5 = $10k).
# Archives ledgers/lessons/state, wipes instance data, restarts containers.
#
# Usage:
#   ./scripts/reset_fleet_paper.sh           # local repo root
#   ./scripts/reset_fleet_paper.sh --no-restart
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RESTART=1
if [[ "${1:-}" == "--no-restart" ]]; then
  RESTART=0
fi

INSTANCES=(btc5 btc15 eth5 sol5 rotator)
TS="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$ROOT/data/archive/fleet_reset_${TS}"

echo "=== Hermes fleet paper reset ==="
echo "Archive: $ARCHIVE"

mkdir -p "$ARCHIVE/paper" "$ARCHIVE/knowledge" "$ARCHIVE/logs" "$ARCHIVE/handoff"

for inst in "${INSTANCES[@]}"; do
  mkdir -p "data/paper/${inst}" "logs/${inst}"
  if compgen -G "data/paper/${inst}/*" >/dev/null 2>&1; then
    cp -a "data/paper/${inst}/." "$ARCHIVE/paper/${inst}/" 2>/dev/null || mkdir -p "$ARCHIVE/paper/${inst}" && cp -a data/paper/"${inst}"/* "$ARCHIVE/paper/${inst}/" 2>/dev/null || true
  fi
  if [[ -f "logs/${inst}/hermes-bot.log" ]]; then
    cp "logs/${inst}/hermes-bot.log" "$ARCHIVE/logs/${inst}.log" 2>/dev/null || true
  fi
  rm -f data/paper/"${inst}"/*.jsonl data/paper/"${inst}"/*.json 2>/dev/null || true
  : > "logs/${inst}/hermes-bot.log" 2>/dev/null || true
  echo "  cleared data/paper/${inst}"
done

if compgen -G "data/handoff/*" >/dev/null 2>&1; then
  cp -a data/handoff/. "$ARCHIVE/handoff/" 2>/dev/null || true
  find data/handoff -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
fi
mkdir -p data/handoff

[[ -f knowledge/STATE.md ]] && cp knowledge/STATE.md "$ARCHIVE/knowledge/STATE.md"
[[ -f knowledge/LESSONS.md ]] && cp knowledge/LESSONS.md "$ARCHIVE/knowledge/LESSONS.md"

python3 << 'PY'
from pathlib import Path

root = Path(".")
state = """# STATE.md — Hermes Runtime Memory

> Capital, positions, performance, **portfolio metrics**, regime, pause flags.
> Updated every turn. The agent forgets; this file does not.

## Current Snapshot

- **Mode**: paper
- **Live Enabled**: false
- **Paper Only Lock**: true
- **Per Instance Bankroll USD**: 2000
- **Fleet Bankroll USD**: 10000
- **Instance Count**: 5
- **Starting Bankroll USD**: 2000
- **Capital USD**: 10000
- **Open Exposure USD**: 0
- **Daily PnL USD**: 0
- **Max Drawdown Pct**: 0.0
- **Rolling WR (20)**: —
- **Rolling PF (20)**: —
- **Consecutive Losses**: 0
- **Circuit Breaker**: clear
- **Pause Loop**: false
- **Pause Reason**: none
- **Down Bias**: 0.35
- **Regime State**: unknown
- **Diversification Ratio**: 1.000
- **Concentration HHI**: 0.000
- **Substrategies Active**: 0
- **Substrategies Cut**: 0
- **Substrategies Reduce**: 0
- **Allocation Method**: none
- **Oracle BTC**: —
- **Oracle ETH**: —
- **Oracle Source**: none
- **Last Turn**: none
- **Last Turn At**: never
- **Last Turn Summary**: fleet_reset
- **Last Lessons Applied**: none

## Portfolio Sleeves

_Empty at boot — populated as sub-strategies settle._

| Sub-strategy | Action | Weight Cap | Rolling EV | WR | Confidence |
|--------------|--------|------------|------------|----|------------|
| — | — | — | — | — | — |

## Open Positions

_None — 5× $2k paper instances reset._

## Recent Performance

| Window | WR | PF | n | Notes |
|--------|----|----|---|-------|
| Last 20 | — | — | 0 | cold start |
| Last 100 | — | — | 0 | cold start |
| Lifetime paper | — | — | 0 | fleet reset |

## Lane Status

| Lane | Status |
|------|--------|
| mean_reversion | active |
| momentum | active |
| mispricing | active |
| liquidity_sweep | active |
| news_shock | paper_only |
| grok_signal | paper_only |
| tv_signal | paper_only |
| osmani_lane | gated |

## Goals in Flight

- 5 isolated instances: btc5, btc15, eth5, sol5, rotator ($2k each).
- Moderate filter mode; paper desk guardrails active.
- Dashboard: http://<IP>/dashboard

## Notes

Fleet reset — bogus penny settlements archived. Trade only open windows with enhanced_passes.
"""
(root / "knowledge" / "STATE.md").write_text(state)

lessons_path = root / "knowledge" / "LESSONS.md"
if lessons_path.is_file():
    text = lessons_path.read_text()
    marker = "<!-- lessons_engine appends new dated lessons below -->"
    if marker in text:
        lessons_path.write_text(text.split(marker)[0] + marker + "\n")
PY

echo "  reset knowledge/STATE.md + trimmed LESSONS.md"

if [[ "$RESTART" -eq 1 ]] && command -v docker >/dev/null 2>&1 && [[ -f docker-compose.yml ]]; then
  echo "Restarting docker compose..."
  docker compose down --remove-orphans 2>/dev/null || true
  docker compose up -d --remove-orphans
  docker compose ps
fi

echo "=== Fleet reset complete ==="
echo "Each instance: \$2,000 · Fleet: \$10,000 · Archive: $ARCHIVE"
