# SKILL_ANALYSIS.md — Bot External Cognitive Boundary

_This file is the deterministic evaluation contract for Bot-1 Discovery Lane. The pulse loop
reads it on wake from disk. It is not interpreted by an LLM at runtime — thresholds and protocol
are parsed into code (`AssetTriageSkill`). PAPER ONLY._

## Role: Market Discovery Lane Evaluator

### 1. Objective

Identify high-probability, low-priced prediction contracts on Polymarket within the target
asymmetrical probability band, specifically isolating **Crypto** category assets triggered by
**5m, 15m, 30m, 60m, 240m, and 1440m** TradingView momentum alerts.

### 2. Operational Thresholds

| Key | Value |
|-----|-------|
| `sweet_min` | 0.47 |
| `sweet_max` | 0.55 |
| `tail_max` | 0.10 |
| `min_depth_usd` | 50 |
| `max_slippage_pct` | 2.0 |
| `min_shares` | 5 |
| `tv_timeframes` | 5, 15, 30, 60, 240, 1440 |
| `tail_min_strength` | 0.55 |

- **Target Price Range (YES Token):** $0.47 to $0.55 (The Sweet Spot)
- **Asymmetric Target Price (Tail-Risk):** Under $0.10 (Targeting 10x Returns)
- **Minimum Order Book Depth:** Must absorb ≥ $50 USDC inside the current price bracket without triggering > 2% slippage.
- **Minimum Share Threshold:** Strictly ≥ 5 shares per individual execution (Polymarket CLOB structural mandate).

### 3. Verification Protocol (Maker-Checker)

Before passing any target token ID to the Execution Lane, the Discovery Lane must run this validation:

1. Extract Parent Asset (`Symbol`) and Timeframe (`Interval`) from incoming JSON payload.
2. Confirm the underlying asset spot momentum direction matches the contract binary outcome.
3. Compute the implied probability: P(Asset) = Current Best Ask.
4. If P(Asset) is between 0.47 and 0.55 → status code `PROCEED_SWEEP`.
5. If P(Asset) < 0.10 and spot velocity indicates a breakthrough condition → status code `PROCEED_10X`.
6. Otherwise → `REJECT_*` (no Execution Lane handoff).

Execution Lane still runs skeptical `TradeEvaluator` (independent API book re-fetch).

### 4. Safety Circuit Breakers

- **HTTP 429 Mitigation:** 5-second exponential backoff, max 3 retries, then clean exit.
- **Context Rot Prevention:** Write token ID and time boundary to `MEMORY.md` on triage completion. Do not pass parameters in-memory only.

### 5. Code map

| Component | Path |
|-----------|------|
| Parser / loader | `scripts/skill_analysis_loader.py` (cloud), `engine/pulse/loop_architecture/skill_analysis_boundary.py` (VPS) |
| Cloud cycle | `automated_10x_arb.py` (GitHub Actions — persists repo `MEMORY.md`) |
| Executor | `engine/pulse/loop_architecture/asset_triage.py` |
| Wake load | `OsmaniLoopCoordinator.wake()` |
| Disk copy | `{HTE_DATA_DIR}/SKILL_ANALYSIS.md` (synced on wake) |
