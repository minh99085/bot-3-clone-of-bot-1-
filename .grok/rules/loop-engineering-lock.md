# Loop Engineering architecture lock (operator mandate — 2026-07-07)

**Do NOT change Loop Engineering architecture autonomously.** Ask the operator for explicit
permission **in the current message** before any edit that alters the loop framework itself.

This lock protects the **structure** of the self-improve loop — not day-to-day gate tuning,
strategy params, or evidence-backed babysit adjustments inside the existing loop.

## What counts as Loop Engineering architecture (PAUSE + ASK)

- Adding, removing, or merging **lanes** (Discovery / Execution / Ledger) or their responsibilities
- Replacing or bypassing **maker-checker** (TradeGenerator ↔ TradeEvaluator) with a new authority path
- Rewriting `engine/pulse/loop_architecture/` coordinator, circuit breaker, or lane wiring
- Introducing a new outer optimizer (e.g. `optimizer_loop.py`, genetic mutators, composite-reward
  VERIFY) that changes how the bot mutates its own code or env
- Changing **MEMORY.md schema** or cross-wake persistence contract in a breaking way
- Major refactors of `docs/osmani-loop-architecture.md` patterns into a different loop model
- Swapping babysit **evaluate → apply** policy structure (not single-key WR tunes inside policy)

Canonical references:

- `docs/osmani-loop-architecture.md`
- `engine/pulse/loop_architecture/`
- `Loop-Engineering-The-Complete-Guide-v260615.pdf` (repo root)
- `.grok/rules/self-improve-loop.md` (runtime adjust layer — tune inside, don't replace)

## Related lock (also operator-approval only)

- **Cross-horizon shared learn policy** (`CrossHorizonLearner`, 15m↔1h restrict/size overlays):
  frozen separately — see `.grok/rules/cross-horizon-learn-lock.md`. Do not redesign transfer
  rules, bucket taxonomy, or force-trade semantics without approval in the current message.

## Safe without architecture approval (proceed autonomously)

- Gate thresholds, sizing, sweet-band blocks, stop guards inside existing modules
- `evaluate-cycle.py` / `apply-wr-tune.py` **parameter** changes within current policy JSON
- `dep_arb_learning.py`, `loop_synthesis.py` evidence rules that tighten/loosen **within** current lanes
- Evidence-backed knobs on the **existing** CrossHorizonLearner env surface (not redesign)
- Bug fixes that preserve lane boundaries and maker-checker semantics
- Docs that describe current architecture without changing behavior
- Cloud babysit observe cycle (pull VPS, write MEMORY.md, publish report)

## Required response (before any architecture edits)

1. **What** architectural piece changes (lanes, coordinator, optimizer, MEMORY contract)
2. **Why** — gap vs current Osmani loop; what problem the redesign solves
3. **Risk** — regression to paper PnL, soak baseline, VPS state, or frozen invariants
4. **Alternative** — can the goal be met with a param/gate change inside the existing loop?
5. **Ask:** "Proceed with this Loop Engineering architecture change?" — wait for explicit yes

## Operator override

Proceed only when the operator says yes in the **current message**, e.g. "yes change the loop
architecture", "implement optimizer_loop", "merge the lanes". Update this file or `CLAUDE.md` only
if the operator also asks to lift the lock.
