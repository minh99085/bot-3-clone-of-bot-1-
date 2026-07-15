# ALPHA_RESEARCH_SKILL.md

> Alpha-specific living skill. Read by `signal_generator` and `verifier` every turn.
> Grows via `lessons_engine` promotions from LESSONS.md.

## Objective

Produce signals with positive live EV after realistic fees + slippage, in buckets
with historical WR ≥ 65%, so the verifier can pass a sparse set of high-quality
trades rather than a firehose of mediocre ones.

**Win rate target:** 80%+ on settled trades (paper → live).
**Expectancy:** live EV ≥ 0.06 (prefer ≥ 0.08).
**Profit factor:** ≥ 1.4. **Max DD:** < 8%.

## Regime Filters

Trade only when regime ∈ {`mean_revert`, `trending_up`, `trending_down`, `low_vol`}.
Reject `unknown`. Be defensive in `high_vol` (require tier A + EV ≥ 0.08).

Multi-timeframe check (verifier enforces):

1. Higher timeframe regime not violently opposing the signal direction
2. Hourly bucket not on the AVOID list
3. Pre-entry stability OK (price wobble within threshold)

## Confidence Tiers

| Tier | Conviction | Verifier |
|------|------------|----------|
| A | ≥ 0.75 | Eligible |
| B | ≥ 0.55 | Eligible |
| C | ≥ 0.35 | **REJECT** |
| D | < 0.35 | **REJECT** |

## Entry Modes

| Mode | Status | Notes |
|------|--------|-------|
| `mean_reversion` | ACTIVE | Preferred with DOWN bias in mid-day buckets |
| `momentum` | ACTIVE | Require clean trend regime |
| `liquidity_sweep` | ACTIVE | Need tight spread + stability |
| `news_shock` | PAPER_ONLY | Until event-study sample ≥ 30 |
| `grok_signal` | PAPER_ONLY | External LLM signal — verify hard |
| `tv_signal` | PAPER_ONLY | TradingView webhook — verify hard |
| `osmani_lane` | **GATED** | Kill/gate until backtest WR > 65% and +EV |

### osmani_lane gate (Hermes weakness)

Do **not** promote to ACTIVE without:

- Walk-forward backtest WR > 65%
- Profit factor > 1.4
- Positive EV after 100 bps fees + 40 bps slippage
- Sample n ≥ 40 in the exact bucket

## DOWN Bias (explicit + dynamic)

DOWN/NO bias is a first-class parameter, not a buried constant.

- Base bias from STATE.md `Down Bias` (default 0.35)
- `trending_down`: bias += 0.25
- `trending_up`: bias -= 0.40 (can go slightly negative → allow YES)
- `high_vol`: bias += 0.10 (more defensive)
- Signal generator tilts YES vs NO edges by this bias before proposing

## Edge Buckets — EXPLOIT

| Regime | Hour (UTC) | Mode | Tier | Dir bias | n | WR | Edge | PF | Action |
|--------|------------|------|------|----------|---|----|------|----|--------|
| mean_revert | 14 | mean_reversion | A | DOWN | 48 | 78% | 0.09 | 1.90 | EXPLOIT |
| trending_down | 20 | momentum | B | DOWN | 35 | 71% | 0.07 | 1.55 | EXPLOIT |

## Edge Buckets — AVOID

| Regime | Hour | Mode | Reason |
|--------|------|------|--------|
| high_vol | * | osmani_lane | Unproven; gated |
| unknown | * | * | No regime filter pass |
| * | * | * | Any LESSONS.md `AVOID:` match |

## Execution Quality (drag fixes)

- `entry_vwap_target`: sit **inside** the spread by ~50 bps — never chase
- `pre_entry_stability_ok`: required true (wobble proxy from spread)
- Abort if live book moves > stability threshold between verify and send
- Fees assumption: 100 bps effective; slippage: 40 bps

## Daily / Rolling Performance Gates

If any trip, pause the loop (STATE.md `Pause Loop: true`):

- Rolling WR(20) < 55%
- Rolling PF(20) < 1.2
- Daily loss > 3% capital
- Drawdown ≥ 8%
- Consecutive losses ≥ 4

## Research Cadence

- Research worktree: backtests, bucket refreshes, lane promotion proposals
- Signal worktree: live/paper signal gen + verify
- Risk worktree: 30s monitor — never blocks execution path

## Auto-Promoted Rules

<!-- lessons_engine appends below this heading -->
