# STATE.md — Hermes Runtime Memory

> Current capital, positions, performance, regime, and pause flags.
> Updated by the loop every turn. The agent forgets; this file does not.

## Current Snapshot

- **Mode**: paper
- **Live Enabled**: false
- **Capital USD**: 10000
- **Open Exposure USD**: 0
- **Daily PnL USD**: 0
- **Max Drawdown Pct**: 0.0
- **Rolling WR (20)**: 100%
- **Rolling PF (20)**: 2.00
- **Consecutive Losses**: 0
- **Circuit Breaker**: clear
- **Pause Loop**: false
- **Pause Reason**: none
- **Down Bias**: 0.35
- **Regime State**: unknown
- **Last Turn**: none
- **Last Turn At**: never
- **Last Turn Summary**: boot
- **Last Lessons Applied**: none

## Open Positions

_None — paper book empty at boot._

## Recent Performance

| Window | WR | PF | n | Notes |
|--------|----|----|---|-------|
| Last 20 | — | — | 0 | cold start |
| Last 100 | — | — | 0 | cold start |
| Lifetime paper | — | — | 0 | cold start |

## Lane Status

| Lane | Status |
|------|--------|
| mean_reversion | active |
| momentum | active |
| liquidity_sweep | active |
| news_shock | paper_only |
| grok_signal | paper_only |
| tv_signal | paper_only |
| osmani_lane | gated |

## Goals in Flight

- Boot goal: collect ≥ 20 paper settlements with verifier-pass provenance before considering live.
- High-conviction pattern: `@goal` → 3+ verified signals or 48h pass.

## Notes

Cold start: verifier REJECT/DEFER live-API signals without matching edge buckets.
Run `python -m hermes.hermes_loop demo` for a synthetic PASS → execute → lesson turn.
