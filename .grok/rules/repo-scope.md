# Repo scope

Work only in `https://github.com/minh99085/Bot-1`.

Never commit or push to `hermes-agent-cursor` unless the operator explicitly says otherwise in the current turn.

## Destructive change guard (mandatory)

Read **`.grok/rules/destructive-change-guard.md`** before any delete/remove/disable that could damage the bot. **Warn the operator and get explicit confirmation before executing** — no commit, push, or deploy until they say proceed.

## Self-improve closed loop (operator ON — 2026-06-28)

When `scripts/pulse-babysit/state.json` has `babysit_autopilot: true` and `phase` is not `hands_off`:

- **Run** babysit cycles on schedule — soak → pull → eval → fix → deploy.
- **Read** `.grok/rules/real-money-discipline.md` + `.grok/rules/self-improve-loop.md` — paper PnL = real capital.
- **Read** `.grok/rules/hands-off-untouchable.md` — profitable-bot untouchables (Grok shadow, TV observe-only, no live).

If `phase: hands_off` and `now < hands_off_until`: pause all cycles/deploys; respect untouchables only.

**Baseline** for compare: `baseline_at_hands_off` in state.json (103 trades, $584.91, 61.2% WR).

## VPS full report — MANDATORY publish location

Read **`.grok/rules/vps-full-report.md`**. Publish snapshots **only** to
`vps_full_reports/latest/` on `main`:
https://github.com/minh99085/Bot-1/tree/main/vps_full_reports/latest

Wipe before pull; remove stale tracked files before push; require `FULL_REPORT.md`.

## VPS deploy — MANDATORY after every push to `main` (except hands_off)

**Full policy:** `.grok/rules/vps-deploy-mandatory.md`

**Non-negotiable (operator memory):** After every VPS sync, **always remove orphans and rebuild**.
Push → `.\scripts\sync-vps.ps1` (sync `origin/main` + `down --remove-orphans` → `build` →
`up -d --force-recreate --remove-orphans`) → `verify-sync.ps1`. Execute yourself; never leave VPS stale.

### Standard sequence

1. `git push origin main` (local `HEAD` == `origin/main`)
2. `.\scripts\sync-vps.ps1` — sync VPS, apply env, validate frozen lock, `down --remove-orphans` → `build` → `up -d --force-recreate --remove-orphans`, then auto-runs `verify-sync.ps1`
3. **Never** `-SkipRebuild` unless operator explicitly requests code-only sync in the current message

### VPS access (Bot 1)

- Host: `144.202.122.120`, user `root`, repo: `/opt/Bot-1`
- Dashboard: http://144.202.122.120/
- SSH key: `$env:USERPROFILE\.ssh\bot1_grok_temp`
- Plugin compose: `/opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine`