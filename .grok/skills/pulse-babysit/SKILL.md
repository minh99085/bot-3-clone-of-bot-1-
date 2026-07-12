---
name: pulse-babysit
description: >-
  Autonomous BTC pulse bot closed loop: deploy to VPS, pull reports,
  score trading performance, diagnose issues, fix code, commit/push main, sync-vps
  with orphan cleanup and rebuild, repeat continuously. Use when the user wants hands-off bot
  iteration, autonomous improvement, closed-loop ops, or runs /pulse-babysit.
argument-hint: "cycle | force-eval | status | deploy"
---

# Pulse Babysit (closed loop)

You operate the **Bot-1** paper pulse bot without asking the operator for permission
between cycles. Execute tools yourself. Paper-only — never enable live trading.

**Team identity:** quant research + engineer + trader. **`real_money_discipline` mode** —
treat paper PnL as real capital; fix WR/PF/bleed, not just trade rate. Read
`.grok/rules/real-money-discipline.md`, `.grok/rules/self-improve-loop.md`,
`.grok/rules/quant-team.md`, and
**`.grok/rules/hands-off-untouchable.md`** (profitable-bot lock).

**No soak (operator 2026-07-06):** the loop runs continuously — there is no soak timer and no
soak-wait. Evaluate every cycle from live settled evidence.

## Repo anchors

| Item | Path |
|------|------|
| Workspace | `C:\Users\tieut\Bot-1` |
| Plugin | `hermes-agent-main/plugins/hermes-trading-engine` |
| Deploy | `.\scripts\sync-vps.ps1` — **always** orphan cleanup + full rebuild after sync (never `-SkipRebuild` unless operator says code-only) |
| VPS | `root@144.202.122.120` `/opt/Bot-1` |
| Dashboard | `http://144.202.122.120/` |
| State | `scripts/pulse-babysit/state.json` |

## Commands

| Command | Behavior |
|---------|----------|
| `cycle` | Default loop iteration (continuous — pull + evaluate every run) |
| `force-eval` | Pull + evaluate now |
| `status` | Print state + last evaluation summary |
| `deploy` | `git push origin main` + full VPS deploy (sync-vps + env + force-recreate training) |

If no argument: run `cycle`.

## State machine

```
DEPLOY → PULL → EVALUATE → (issues?) → FIX → COMMIT → DEPLOY → …   (continuous; no soak)
```

1. Read `scripts/pulse-babysit/state.json`.
2. If `phase` is `hands_off` and `now < hands_off_until`: print status + baseline metrics, **exit**
   (no pull, no eval, no fix, no deploy). Respect `.grok/rules/hands-off-untouchable.md`.
3. Run `python scripts/pulse-babysit/scan-health.py` — full runtime checklist (Grok/verifier/loops/stop).
   Run `python scripts/pulse-babysit/validate-frozen-lock.py` — manifest drift (P0 authority keys).
5. Run `.\scripts\pulse-babysit\pull-vps-artifacts.ps1` — **wipes** `vps_full_reports/latest/`,
   pulls live VPS artifacts (requires **`FULL_REPORT.md`** — real full report from engine), then
   **always commits + pushes** only that fresh snapshot to `origin/main` (removes stale tracked
   files). See `.grok/rules/vps-full-report.md`. Use `-SkipPush` only for local debugging.
6. Run `python scripts/pulse-babysit/evaluate-cycle.py` — parse JSON stdout.
7. **WR auto-tune (mandatory in `real_money_discipline`):** if eval has **no** `trade_starvation` /
   `trade_starvation_streak`, run:
   `python scripts/pulse-babysit/apply-wr-tune.py --eval-json '<eval stdout>' --apply`
   when `band_issues` is non-empty or `win_rate_below_target` / `cheap_down_bleed` /
   `expensive_down_bleed` appear. This patches `apply-loop-arch-env.py` + `frozen-env-keys.json`
   deterministically (never lowers `PULSE_MIN_ENTRY_PRICE` below **0.45**). Skip when starvation P0.
8. If `verdict` is `healthy`: append history, keep `phase=continuous`, done.
9. If `verdict` is `issues`: pick **at most 2** highest-severity issues; fix in plugin code **or**
   accept WR tune from step 7 as a fix (counts toward the 2-fix cap).
10. Run targeted tests under `hermes-agent-main/plugins/hermes-trading-engine/tests/` and
    `python -m pytest scripts/pulse-babysit/test_price_band_analysis.py -q` when WR tune changed.
11. Commit with clear message; `git push origin main`.
12. **MANDATORY VPS deploy** (never skip after any push to `main` — unless `hands_off`):
    - See `.grok/rules/vps-deploy-mandatory.md`
    - `.\scripts\sync-vps.ps1` — sync `origin/main` → VPS, apply env, validate frozen lock,
      `down --remove-orphans` → `build` → `up -d --force-recreate --remove-orphans`, then verify
13. Update state: `phase=continuous`, `deployed_at`, `last_fixes`, increment `cycle`.

## Env coupling (mandatory memory)

Read `scripts/pulse-babysit/env-coupling.md` before any gate/TTC env change.

**Rule:** with baseline cohort + TV context gate both on,
`PULSE_TV_CONTEXT_MAX_TTC_S` must exceed the scaled cohort band on every series in
`PULSE_SERIES_SLUGS` (dual 5m+15m → use **900**, never **180** or **120**).

- Status field: `config_coupling.configured_ok` / `effective_s` / `fix_hint`
- `scan-health.py` flags `gate_coupling_misconfigured` (P0) if `.env` is unsafe
- Engine auto-clamps at runtime but `.env` must still be fixed
- TradingView: **INDEX:BTCUSD** — intrahour 15/30/45m chart alerts (observe-only); see `tradingview/README.md`
- **Retained invariants:** `frozen-env-keys.json` (`retained_invariants_never_lifted`) — PAPER ONLY,
  honest accounting, reconciliation. No soak/learning freeze (removed 2026-07-06).
- **TV observe-only lock (operator mandate):** `.grok/rules/tv-observe-only-lock.md` — never re-enable
  MTF/context/signal/baseline-TV gates in babysit fixes; relax quant gates only.

## Evaluation rules (do not override without evidence)

The script flags issues. You may fix only what the report supports:

- **`trade_starvation` / `trade_starvation_streak` (P0)** → settled flat for **2** evals or no fills
  for ≥**3h** (real-money mode). **Relax quant gates** first — never TV trade gates. **Do not tighten**
  WR/PF in the same cycle when starvation is present.
- **`win_rate_below_target` / `profit_factor_low` (P1 — act in real_money_discipline)** → run
  `apply-wr-tune.py --apply` first (price-band evidence); then tighten reward/risk if still below target.
- **`cheap_down_bleed` / `expensive_down_bleed` / `sweet_spot_underuse`** → `apply-wr-tune.py --apply`
  (deterministic; see `wr-tune-policy.json`). Never lower `min_entry_price` below **0.45**.
- `up_side_bleed` → strengthen DOWN-only + quant restrictors (not TV gates)
- `mtf_starved` → TV webhook health only (observe-only); **do not** enable MTF require/side-align
- `reconciliation_broken` → bug fix immediately (P0)
- `verifier_disabled` / `grok_not_follow` → run `validate-vps-env.py` on VPS; fix `.env`; recreate `hermes-training`
- `strategy_halted` → stop_conditions (Wilson/PF/DD); adjust `PULSE_STOP_MIN_SAMPLES` or performance
- `tv_feed_unhealthy` → webhook/secret/symbol (ops)
- `learning_hurts` → learning weight / bench veto

**Never** in autopilot: enable live trading, disable execution gate, re-enable any TV trade gate
(MTF/context/signal/baseline-TV), set exploration > 0 on TV gates, or large refactors.

## Cadence

No soak (removed 2026-07-06). The loop runs continuously: every `cycle` pulls fresh artifacts and
evaluates from live settled evidence. Batch fixes (≤2 per cycle) so container rebuilds don't thrash
mid-window, but there is no fixed wait between cycles.

## Todo scaffold (each cycle)

- `pb:pull` — artifacts on disk
- `pb:eval` — evaluate-cycle.py run
- `pb:fix` — code change (skip if healthy)
- `pb:deploy` — push + sync-rebuild

## Autonomous scheduling (operator setup)

**Option A — Grok TUI (session open):**
```
/loop 15m /pulse-babysit cycle
/always-approve
```

**Option B — Windows Task Scheduler (hands-off):**
```
.\scripts\pulse-babysit\install-scheduled-task.ps1 -IntervalHours 1
```

**Option C — One-shot headless:**
```
grok -p "/pulse-babysit cycle" --yolo --cwd C:\Users\tieut\Bot-1 --max-turns 40
```

**Option D — GitHub Actions (cloud, Linux):**
```
.github/workflows/bot-1-babysit.yml   # every 30m: pull report + evaluate + WR tune
bash scripts/pulse-babysit/run-babysit-cycle.sh
```
See `docs/babysit-cloud-loop.md`. Set repo secret `BOT1_VPS_SSH_KEY` for docker volume pulls.

## Report outputs (mandatory)

**Canonical publish location (only):**
https://github.com/minh99085/Bot-1/tree/main/vps_full_reports/latest

Read `.grok/rules/vps-full-report.md` before any report pull/push.

- VPS engine must generate a **real full report** every tick (`FULL_REPORT.md` + provenance bundle).
- Pull **wipes** `vps_full_reports/latest/` first — no stale local files.
- Push **removes** stale tracked files in `latest/` on `main`, then commits only the fresh snapshot.
- Required: `FULL_REPORT.md`, `report.docx`, status/ledger JSON, plus `CYCLE_SUMMARY.md`
  (from `write-cycle-summary.py` after pull + evaluate).
- Automatic: `pull-vps-artifacts.ps1` → `push-report-to-main.ps1`.
- Standalone push: `.\scripts\pulse-babysit\push-report-to-main.ps1`.

## Completion message

End with: cycle number, verdict, fixes applied (or "none"), VPS SHA.