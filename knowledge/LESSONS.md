# LESSONS.md — Self-Improving Memory

> Every loss, rejection, or near-miss adds a dated, actionable rule.
> Verifier reads this file. Lessons that stop holding are retired with evidence.

## How to write a lesson

- **Imperative rule** (AVOID:… / EXPLOIT:… / REQUIRE:…)
- **Evidence** (signal id, market, numbers)
- **Applies to** (mode / regime / hour / tier)
- **Promote to** ALPHA_RESEARCH_SKILL or SKILL when durable
- **Retired** false until evidence flips

## Seed Lessons (Hermes v1 post-mortem)

### [2026-07-15] `les_seed_osmani` — CRITICAL (rejection)

- **Rule**: AVOID:osmani_lane in all regimes until walk-forward WR > 65% and +EV after fees. Treat as GATED.
- **Evidence**: Hermes v1 reports — lane underperformed; execution drag + weak regime guards.
- **Applies to**: osmani_lane, high_vol
- **Promote to**: ALPHA_RESEARCH_SKILL
- **Retired**: false

### [2026-07-15] `les_seed_exec_drag` — HIGH (near_miss)

- **Rule**: REQUIRE:entry_vwap_target inside spread and pre_entry_stability_ok=true before PASS. No chase fills.
- **Evidence**: Hermes v1 execution drag from loose VWAP and missing stability filter.
- **Applies to**: momentum, mean_reversion, liquidity_sweep
- **Promote to**: ALPHA_RESEARCH_SKILL
- **Retired**: false

### [2026-07-15] `les_seed_down_bias` — MEDIUM (rejection)

- **Rule**: REQUIRE:dynamic DOWN bias from STATE.md; do not hardcode a static YES preference.
- **Evidence**: Hermes v1 — DOWN bias was implicit/static and drifted wrong in trending_up.
- **Applies to**: direction, regime
- **Promote to**: ALPHA_RESEARCH_SKILL
- **Retired**: false

### [2026-07-15] `les_seed_perf_gates` — HIGH (risk_halt)

- **Rule**: AVOID:trading when rolling WR(20) < 55% or PF(20) < 1.2 — pause the loop.
- **Evidence**: Hermes v1 lacked daily/rolling performance gates that can pause automation.
- **Applies to**: risk_monitor, hermes_main
- **Promote to**: SKILL
- **Retired**: false

### [2026-07-15] `les_seed_hour_guards` — MEDIUM (rejection)

- **Rule**: REQUIRE:hourly_bucket + confidence_tier guards on every signal; reject unknown hour/regime combos without bucket history.
- **Evidence**: Hermes v1 weak hour/regime/confidence guards.
- **Applies to**: discovery, verifier
- **Promote to**: ALPHA_RESEARCH_SKILL
- **Retired**: false

## Active Lessons

<!-- lessons_engine appends new dated lessons below -->

### [2026-07-15] `les_81c6511b92da` — LOW (settlement)
- **Rule**: EXPLOIT continues: mean_reversion / mean_revert / h14 / A produced a win (pnl=$44.90). Keep sizing rules; do not loosen EV gate.
- **Evidence**: signal=sig_demo market=mkt_fed_cut won=True mode=mean_reversion regime=mean_revert
- **Applies to**: mean_reversion, mean_revert, h14, A
- **Promote to**: none
- **Retired**: false
