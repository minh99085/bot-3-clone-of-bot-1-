# Bot-1 — project rules

## Cursor Cloud (`bot-3-clone-of-bot-1-`)

Flat copy of `https://github.com/minh99085/Bot-1` in `/workspace` (no nested clone folder).

**Re-sync from upstream:**

```bash
git clone https://github.com/minh99085/Bot-1 /tmp/bot-1
tar -C /tmp/bot-1 --exclude='.git' -cf - . | tar -C /workspace -xf -
```

**Install / test (VM baseline: Python 3.12, Node.js v22):**

```bash
cd hermes-agent-main/plugins/hermes-trading-engine
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest
```

---

## Quant team mandate (ALWAYS follow)

Operate as a **quant research + engineer + trader** team targeting **~80% WR** on selective entries.
Each cycle: read live performance → hypothesize from market + bot data → implement minimal gate/strategy
changes → measure on live settled outcomes (continuous loop). See `.grok/rules/quant-team.md`.

## Roan / Bregman architecture (Phase 0+)

5m brain, 15m hands — `docs/roan-bregman-architecture.md`. Promotion gates:
`scripts/pulse-babysit/roan-bregman-promotion-scorecard.json`.
**Promotion gate LIFTED 2026-07-01 (operator "remove all locks"):** `PULSE_DEPENDENCY_ARB_EXECUTE`
and `PULSE_BREGMAN_TRADE_AUTHORITY` are now ON; the loop learns/grades their edge from live outcomes.
Still **PAPER ONLY**.

## Loop Engineering architecture lock (OPERATOR 2026-07-07)

**Do not change Loop Engineering architecture without explicit operator approval in the current
message.** Lanes (Discovery / Execution / Ledger), maker-checker, `loop_architecture/` coordinator,
outer optimizer design, and MEMORY.md contract are frozen. Autonomous work stays inside the
existing loop: gates, sizing, dep-arb learning, babysit WR tunes. Full scope:
`.grok/rules/loop-engineering-lock.md`.

## Cross-horizon shared learn policy lock (OPERATOR 2026-07-12)

**Do not redesign `CrossHorizonLearner` (15m↔1h shared restrict/size policy) without explicit
operator approval in the current message.** Transfer rules, bucket taxonomy, Wilson promote/demote
semantics, and execution-gate authority stay frozen. Evidence-backed babysit knobs on the existing
env surface remain in scope. Full scope: `.grok/rules/cross-horizon-learn-lock.md`.

## Soak removed (OPERATOR 2026-07-06)

**The soak is removed entirely** (operator "remove all the soak"). There is no soak-wait discipline,
no learning-collection freeze, and no soak timer — the closed loop runs continuously and governs
gates/sizing/authority from live settled evidence. Still run `validate-frozen-lock.py` before deploy;
it now enforces only the **retained invariants** in `scripts/pulse-babysit/frozen-env-keys.json`
(`retained_invariants_never_lifted`): PAPER ONLY, honest accounting, reconciliation.

## TradingView observe-only lock (OPERATOR MANDATE — LIFTED 2026-07-01)

**LIFTED 2026-07-01 (operator "remove all locks").** No longer enforced. NOTE: TV trade gates are
still set to `0` (OFF) in `apply-loop-arch-env.py` on purpose — TV is negative-alpha and currently
stale, so enabling gates would only block trades and starve the learners. TV intake stays on as
observe features; the loop may adjust TV usage from evidence. Detail:
`.grok/rules/tv-observe-only-lock.md`.

## Repository scope (ALWAYS follow)

- **Canonical repo:** `https://github.com/minh99085/Bot-1` — the **only** GitHub repository for code, commits, pushes, reports, and deploys.
- **Do not** clone, commit, or push to `hermes-agent-cursor` or any other repo unless the operator explicitly overrides this in the current message.
- **Local workspace:** prefer `C:\Users\tieut\Bot-1` when working from this machine.
- **Default branch:** `main` — **commit here only; do not create feature branches or PRs**
  (operator rule 2026-07-04; **reiterated 2026-07-07**).
- **VPS deploy (MANDATORY after every push to `main`):** See `.grok/rules/vps-deploy-mandatory.md`.
  **Ship pipeline (always, in order):** push to `main` → sync VPS →
  `docker compose down --remove-orphans` → `build` → `up -d --force-recreate --remove-orphans`.
  No feature branches. No PRs. Job is incomplete until VPS rebuild finishes.

## Project layout

- Trading bot plugin: `hermes-agent-main/plugins/hermes-trading-engine/`
- Full VPS reports: **only** `vps_full_reports/latest/` on `main` — see `.grok/rules/vps-full-report.md`.
  Engine must generate a **real full report** (`FULL_REPORT.md` + provenance bundle). On every pull:
  wipe `latest/`, pull fresh from VPS, remove stale tracked files, commit + push to `origin/main`.
  Canonical URL: https://github.com/minh99085/Bot-1/tree/main/vps_full_reports/latest
- Design townhall: `Design Townhall` (repo root)
- Operator guide for the pulse engine: `hermes-agent-main/plugins/hermes-trading-engine/AGENTS.md`
- Autonomous closed loop: `/pulse-babysit cycle` or `.\scripts\pulse-babysit\install-scheduled-task.ps1` (continuous — no soak; see `.grok/skills/pulse-babysit/SKILL.md`)