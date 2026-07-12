# Cross-horizon shared learn policy lock (operator mandate — 2026-07-12)

**Do NOT change the CrossHorizonLearner shared policy design without explicit operator
approval in the current message.**

This lock freezes the **15m ↔ 1h shared cross-learn contract** shipped with the learner:
restrict/size-only overlays, relative SSO/TTC buckets, Wilson promote/demote, exploration
carve-out, and execution-gate authority. It sits alongside the Loop Engineering architecture
lock (`.grok/rules/loop-engineering-lock.md`).

## What is locked (PAUSE + ASK)

- Adding/removing transfer rules (what 15m teaches 1h and vice versa)
- Changing promote/demote semantics, bucket taxonomy, or Wilson/kill/target thresholds **as a
  redesign** (not evidence-backed babysit param nudges inside existing knobs)
- Letting CrossHorizonLearner force trades, bypass maker-checker, or become a new lane
- Replacing `engine/pulse/cross_horizon_learner.py` with a different optimizer / outer loop
- Turning the shared policy into a side-picker that overrides the tier/execution gate
- Broadening beyond restrict/size overlays without operator approval

Canonical module: `hermes-agent-main/plugins/hermes-trading-engine/engine/pulse/cross_horizon_learner.py`

Env surface (apply via `scripts/apply-loop-arch-env.py`):

- `PULSE_CROSS_HORIZON_LEARN_ENABLED`
- `PULSE_CROSS_HORIZON_MIN_SAMPLES`
- `PULSE_CROSS_HORIZON_TARGET_WR`
- `PULSE_CROSS_HORIZON_KILL_WR`
- `PULSE_CROSS_HORIZON_EXPLORATION_RATE`

## Safe without approval (proceed autonomously)

- Evidence-backed babysit WR tunes of the **existing** env knobs above
- Bug fixes that preserve restrict/size-only + execution-gate authority
- Report/dashboard surfacing of learner state
- Docs describing current behavior

## Required response (before any redesign)

1. **What** changes in the shared policy contract
2. **Why** — gap vs current 15m↔1h transfer
3. **Risk** — paper PnL, 1h starvation, override of execution gate
4. **Ask:** "Proceed with this CrossHorizonLearner policy change?" — wait for explicit yes

## Operator override

Proceed only when the operator says yes in the **current message** (e.g. "yes change the
cross-horizon learn policy"). Update this file only if the operator also asks to lift the lock.
