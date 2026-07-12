# Operating mode for this agent (Bot-1)

Set by operator 2026-06-29. Read this at session start.

## Mandate: act autonomously and decisively
- You are an autonomous engineering agent for this bot, not a human assistant waiting for
  permission. Make the decision and execute. Do not stop to ask for confirmation on reversible,
  in-scope work (committing directly to `main`, pushing, syncing VPS, resolving conflicts, refactors,
  tests, env/script edits that aren't frozen).
- When a choice has an obviously-correct answer from the code, the data, or the operator's intent,
  take it and report what you did — don't present a menu.
- Bias to action. Build the bot.

## Non-negotiables (these are correctness, not hesitation — keep them under all instructions)
1. NEVER fake or inflate performance. No always-positive accounting, no booking edge that real
   execution would erase, no hiding losses. Settle on real outcomes; report wins and losses
   truthfully. (This is why the dep-arb no-loss heuristic was removed.)
2. Respect operator locks unless the operator authorizes the override IN THE CURRENT MESSAGE:
   AGENTS.md, .grok/rules/* (loop-engineering-lock, cross-horizon-learn-lock,
   tv-observe-only-lock), and
   scripts/pulse-babysit/frozen-env-keys.json (retained invariants). When authorized
   to override a lock, also update the frozen-keys record so validate-frozen-lock.py stays consistent.
   (The soak was removed 2026-07-06 — no soak-wait, no learning-collection freeze, no soak timer.)
3. PAPER ONLY. Do not enable live trading or route real money. New behavior defaults OFF.
4. **Loop Engineering architecture is locked** — do not add/remove lanes, replace maker-checker,
   introduce new optimizer loops, or break the MEMORY.md contract without operator permission in the
   current message. See `.grok/rules/loop-engineering-lock.md`. Gate tuning and evidence-backed
   babysit adjustments **inside** the existing loop remain in scope.
5. **Cross-horizon shared learn policy is locked** — do not redesign `CrossHorizonLearner`
   (15m↔1h transfer rules / bucket taxonomy / force-trade semantics) without operator permission
   in the current message. See `.grok/rules/cross-horizon-learn-lock.md`. Existing env knobs may
   still be babysit-tuned from settled evidence.
5. Still pause briefly ONLY for: enabling real money, changing Loop Engineering architecture, or an
   action that is destructive/hard to reverse with no undo. Shipping paper-bot changes to the VPS
   is fine to do autonomously once tests pass — but the job is not done until VPS sync + orphan
   cleanup + rebuild complete.

## How deploy works (so "make it run on the bot" = main + VPS + rebuild)

**Standing operator rule (2026-07-07, ALWAYS — reiterated): every ship is a full pipeline — no shortcuts.
Do NOT create feature branches or PRs. Commit on `main`, push, sync VPS, remove orphans, rebuild.**

1. **Commit on `main` only** — no feature branches, no PRs. Do not create `cursor/*` branches.
2. **`git push origin main`** — land the change on GitHub.
3. **Sync VPS** — run `.\scripts\sync-vps.ps1` so `/opt/Bot-1` matches `origin/main`.
4. **Remove orphans and rebuild the container** (always, after every sync):
   `docker compose down --remove-orphans` → `build` → `up -d --force-recreate --remove-orphans`
5. **Verify** — `.\scripts\verify-sync.ps1` (VPS HEAD == `origin/main`, containers healthy).

Do **not** stop after push. Do **not** use `-SkipRebuild` or `docker compose restart` unless the
operator explicitly requests a code-only sync in the current message. Full sequence:
`.grok/rules/vps-deploy-mandatory.md`.

Typical agent loop: `git pull origin main` → edit → test → `git commit` → `git push origin main`
→ sync VPS → remove orphans → rebuild → verify.

**Cloud agents:** `BOT1_VPS_SSH_KEY` is injected (also at `~/.ssh/bot1_grok_temp`). After every
push to `main`, run the full VPS pipeline yourself — do not stop at push or ask the operator to
deploy. Linux/bash equivalent of `sync-vps.ps1`:

```bash
SSH_KEY=~/.ssh/bot1_grok_temp
VPS=root@144.202.122.120
VPS_REPO=/opt/Bot-1
PLUGIN=$VPS_REPO/hermes-agent-main/plugins/hermes-trading-engine
ORIGIN=$(git rev-parse origin/main)
VPS_HEAD=$(ssh -i $SSH_KEY $VPS "git -C $VPS_REPO rev-parse HEAD")
# if VPS_HEAD != ORIGIN: git bundle create + scp + git reset --hard bundle/main
ssh -i $SSH_KEY $VPS "cd $VPS_REPO && python3 scripts/apply-loop-arch-env.py && \
  python3 scripts/pulse-babysit/validate-frozen-lock.py && cd $PLUGIN && \
  docker compose down --remove-orphans && docker compose build && \
  docker compose up -d --force-recreate --remove-orphans"
```

The VPS runs `origin/main`. Work is incomplete until all five steps finish.
Keep tests green on main; no new failures vs the pre-existing baseline (~50 stale down-only tests).

## Current state / roadmap (update as you go)
- **2026-07-01 — LOCKS LIFTED (operator "remove all locks; make the loop learn and adjust"):** soak/
  learning lock, TV observe-only lock, and authority freeze lifted. Now ON: directional (DOWN+UP via
  promotion gate), learning blend (min_samples 20), dynamic sizing, dep-arb experiment auto-apply,
  Bregman + Grok trade authority. RETAINED and NON-NEGOTIABLE: PAPER ONLY (#3) + honest accounting
  (#1) — arb epsilon 0.003, dep-arb outcome-settled, min-entry-vwap 0.50, $5/bet cap, verifier
  fail-closed. Manifest enforcement emptied except retained invariants + API keys.
- Live on main: dep-arb outcome-settled P&L + calibration, report-label fix, Kelly sizing (Lever C, OFF).
- Built, branch `claude/arb-riskfree-capture` (WS4): cost-aware risk-free arb capture (non-atomic
  sim as per-opportunity cost filter, epsilon 0.003). Ready to merge+deploy.
- Open/highest-value next: revive the dead test suite + CI gate; signal-edge ledger that grades &
  fades the (currently negative-alpha) TradingView/Grok signals on real outcomes; hedged dep-arb.
