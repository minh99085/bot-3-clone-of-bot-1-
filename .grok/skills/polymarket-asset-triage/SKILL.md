---
name: polymarket-asset-triage
description: >-
  Polymarket Asset Triage & Sweet-Spot Evaluation for the Osmani Discovery Lane.
  Identifies crypto prediction contracts in the 0.47–0.55 sweet spot (and <0.10 tail
  10x band) triggered by 15/30/45/55m TradingView momentum alerts. Use when
  implementing or tuning discovery-lane triage, MEMORY.md writes, or maker-checker
  pre-execution validation.
argument-hint: "reference | thresholds | verify"
---

# SKILL: Polymarket Asset Triage & Sweet-Spot Evaluation

**Role:** Market Discovery Lane Evaluator (Lane 1)

**Code:** `engine/pulse/loop_architecture/asset_triage.py`  
**Wired in:** `DiscoveryLane` → `OsmaniLoopCoordinator` → `PulseEngine.osmani_loop`

## 1. Objective

Identify high-probability, low-priced prediction contracts on Polymarket within the
target asymmetrical probability band, specifically isolating **Crypto** category assets
triggered by **15m, 30m, 45m, and 55m** TradingView momentum alerts.

## 2. Operational Thresholds

| Parameter | Value |
|-----------|-------|
| Sweet-spot (YES token ask) | $0.47 – $0.55 |
| Tail-risk 10× band | ask < $0.10 |
| Min book depth (probe) | ≥ $50 USDC at ≤ 2% slippage |
| Min shares | ≥ 5 (Polymarket CLOB mandate) |

Env overrides: `PULSE_TRIAGE_SWEET_MIN`, `PULSE_TRIAGE_SWEET_MAX`, `PULSE_TRIAGE_TAIL_MAX`,
`PULSE_TRIAGE_MIN_DEPTH_USD`, `PULSE_TRIAGE_MAX_SLIPPAGE_PCT`, `PULSE_TRIAGE_MIN_SHARES`.

## 3. Verification Protocol (Maker-Checker)

Before passing a target to Execution Lane:

1. Extract parent asset (`symbol`) and timeframe (`interval`) from TV payload / latest feed.
2. Confirm spot momentum direction matches contract binary side (UP→up, DOWN→down).
3. Implied probability P = current best ask.
4. If P ∈ [0.47, 0.55] and depth OK → `PROCEED_SWEEP`.
5. If P < 0.10, TV breakthrough (strength ≥ 0.55, aligned) and depth OK → `PROCEED_10X`.
6. Else → `REJECT_*` (no execution queue emit).

Execution Lane still runs skeptical `TradeEvaluator` (independent API re-fetch).

## 4. Safety

- **HTTP 429:** exponential backoff from 5s, max 3 retries (`RateLimitGuard`).
- **MEMORY.md:** token_id + time boundary written on each triage completion (disk-bound;
  no in-memory-only handoff).

## PAPER ONLY

Discovery proposes; Execution verifies; Ledger persists. Never live funds.
