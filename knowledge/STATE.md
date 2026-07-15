# STATE.md — Hermes Runtime Memory

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
- **Last Turn Summary**: boot
- **Last Lessons Applied**: none

## Portfolio Sleeves

_Empty at boot — populated as sub-strategies settle._

| Sub-strategy | Action | Weight Cap | Rolling EV | WR | Confidence |
|--------------|--------|------------|------------|----|------------|
| — | — | — | — | — | — |

## Open Positions

_None — 5× $2k paper instances at boot._

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
| mispricing | active |
| liquidity_sweep | active |
| news_shock | paper_only |
| grok_signal | paper_only |
| tv_signal | paper_only |
| osmani_lane | gated |

## Goals in Flight

- 5 isolated instances: btc5, btc15, eth5, sol5, rotator ($2k each = $10k fleet).
- Moderate filter mode; paper desk guardrails active.
- Dashboard: http://<IP>/dashboard

## Notes

**Fleet:** btc5 / btc15 / eth5 / sol5 / rotator — each $2,000 paper bankroll.
