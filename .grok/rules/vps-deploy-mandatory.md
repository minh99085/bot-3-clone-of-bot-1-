# VPS deploy — mandatory sequence (operator rule)

**Operator memory (2026-07-07, ALWAYS — reiterated): always push to `main`, sync VPS,
then remove orphans and rebuild the container.** No exceptions — every code/config change lands on
`main`, syncs to the VPS, and finishes with
`docker compose down --remove-orphans` → `build` → `up -d --force-recreate --remove-orphans`.
Never skip the rebuild. **Do not create feature branches or PRs** (no `cursor/*` branches).

**Operator memory (ALWAYS):** After every VPS sync, **always remove orphans and rebuild** —
`docker compose down --remove-orphans` → `build` → `up -d --force-recreate --remove-orphans`.
Use `.\scripts\sync-vps.ps1` (default; rebuild ON). **Never** `-SkipRebuild` unless the operator
explicitly requests a code-only sync in the current message. No `docker compose restart` shortcuts.

**Non-negotiable:** After every push to `origin/main`, sync the VPS to that SHA, remove orphan containers, and rebuild — unless `state.json` is in `hands_off`.

**Git workflow:** Commit directly on `main` only. Do not create feature branches or PRs.

## Required sequence

1. **Repo ↔ `origin/main` aligned**
   - Local `HEAD` must equal `origin/main` before deploy.
   - If behind: `git pull --ff-only origin main`. If ahead: `git push origin main` first.

2. **VPS ↔ `origin/main` aligned**
   - Run `.\scripts\sync-vps.ps1` from `C:\Users\tieut\Bot-1`.
   - VPS `/opt/Bot-1` HEAD must match `origin/main` after bundle sync.

3. **Orphan cleanup + full rebuild (always)**
   - `python3 scripts/apply-loop-arch-env.py`
   - `python3 scripts/pulse-babysit/validate-frozen-lock.py`
   - `docker compose down --remove-orphans`
   - `docker compose build`
   - `docker compose up -d --force-recreate --remove-orphans`

4. **Verify**
   - `.\scripts\verify-sync.ps1` — VPS HEAD == `origin/main`; containers healthy.

## Never

- Push to `main` and stop without VPS deploy.
- `-SkipRebuild` unless the operator explicitly requests code-only sync in the current message.
- `docker compose restart` or single-service recreate instead of full down → build → up.
- Deploy Bot 1 changes to Bot 2 VPS (or vice versa).

## Bot 1 targets

| Item | Value |
|------|-------|
| Repo | `https://github.com/minh99085/Bot-1` |
| VPS | `root@144.202.122.120` |
| Dashboard | `http://144.202.122.120/` |
| Path | `/opt/Bot-1` |
| Script (Windows) | `.\scripts\sync-vps.ps1` |
| SSH key (cloud agent) | `BOT1_VPS_SSH_KEY` → `~/.ssh/bot1_grok_temp` |

Cloud agents must run the full sync + rebuild sequence themselves after every push to `main`
(see `CLAUDE.md` bash recipe). Do not defer deploy to the operator.