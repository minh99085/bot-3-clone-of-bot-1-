# SKILL_ANALYSIS.md — Bot External Cognitive Boundary

_This file is the deterministic evaluation contract for Bot 3 Discovery Lane. The pulse loop
reads it on wake from disk. It is not interpreted by an LLM at runtime — thresholds and protocol
are parsed into code (`AssetTriageSkill`). PAPER ONLY._

## Role: Market Discovery Lane Evaluator

### 1. Objective

Identify high-probability, low-priced prediction contracts on Polymarket within the target
asymmetrical probability band, specifically isolating **Crypto** category assets. Trend alignment
uses **Chainlink spot** (`PULSE_TRIAGE_TREND_SOURCE=price`); 5m RSI Divergence is confirm/fade
overlay only.

### 2. Operational Thresholds

| Key | Value |
|-----|-------|
| `sweet_min` | 0.48 |
| `sweet_max` | 0.72 |
| `tail_max` | 0.10 |
| `min_depth_usd` | 50 |
| `max_slippage_pct` | 2.0 |
| `min_shares` | 5 |
| `tv_timeframes` | 5 |
| `tail_min_strength` | 0.70 |

- **Target Price Range (YES Token):** $0.48 to $0.72 (aligned with tier sweet band)
- **Asymmetric Target Price (Tail-Risk):** Under $0.10 (Targeting 10x Returns)
- **Minimum Order Book Depth:** Must absorb ≥ $50 USDC inside the current price bracket without triggering > 2% slippage.
- **Minimum Share Threshold:** Strictly ≥ 5 shares per individual execution (Polymarket CLOB structural mandate).

### 3. Verification Protocol (Maker-Checker)

Before passing any target token ID to the Execution Lane, the Discovery Lane must run this validation:

1. Confirm Chainlink spot trend aligns with contract side (rising→UP, falling→DOWN).
2. Flat trend: allow exploration probes at `PULSE_TRIAGE_FLAT_EXPLORATION_RATE` (learning only).
3. Compute the implied probability: P(Asset) = Current Best Ask.
4. If P(Asset) is between 0.48 and 0.72 → status code `PROCEED_SWEEP`.
5. If P(Asset) < 0.10 and spot velocity indicates a breakthrough condition → status code `PROCEED_10X`.
6. Otherwise → `REJECT_*` (no Execution Lane handoff).

Execution Lane still runs skeptical `TradeEvaluator` (independent API book re-fetch).

### 4. Safety Circuit Breakers

- **HTTP 429 Mitigation:** 5-second exponential backoff, max 3 retries, then clean exit.
- **Context Rot Prevention:** Write token ID and time boundary to `MEMORY.md` on triage completion. Do not pass parameters in-memory only.

### 5. Code map

| Component | Path |
|-----------|------|
| Parser / loader | `engine/pulse/loop_architecture/skill_analysis_boundary.py` (VPS) |
| Cloud cycle | (removed — Bot 3 uses VPS loop only) |
| Executor | `engine/pulse/loop_architecture/asset_triage.py` |
| Wake load | `OsmaniLoopCoordinator.wake()` |
| Disk copy | `{HTE_DATA_DIR}/SKILL_ANALYSIS.md` (synced on wake) |
