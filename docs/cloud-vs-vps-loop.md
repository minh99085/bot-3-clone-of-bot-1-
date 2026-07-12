# Cloud vs VPS — where Bot-1 runs

## Canonical runtime: VPS

The **continuous paper trading loop** runs on the Bot-1 VPS:

| Item | Value |
|------|-------|
| Host | `root@144.202.122.120` |
| Dashboard | http://144.202.122.120/ |
| Repo | `/opt/Bot-1` |
| Loop container | `hermes-training` (`scripts/run_btc_pulse.py`) |
| API container | `hermes-trading-engine` |
| Loop memory | `{HTE_DATA_DIR}/MEMORY.md` (Docker volume — **not** in git) |
| Thresholds | `SKILL_ANALYSIS.md` loaded on Osmani wake |

Deploy after every `git push origin main`: `scripts/sync-vps.ps1` (full down → build → up).

## Cloud wing: GitHub Actions

Workflow: `.github/workflows/bot-1_loop.yml`  
Script: `automated_10x_arb.py`

**Does not place live orders.** Every 15 minutes it:

1. Loads thresholds from `SKILL_ANALYSIS.md` (`scripts/skill_analysis_loader.py`)
2. Pulls paper wallet + open trades from VPS HTTP API
3. Runs optional Gamma/CLOB sweet-spot discovery using those thresholds
4. Writes `MEMORY.md` + `LOGS.txt` at repo root (open trades + wallet balances explicit)
5. Commits state to `main` with `[skip ci]`
6. Runs `scan-health.py` as a secondary check

### Why VPS still runs the main loop?

- RTDS feed, Docker volume, 24×7 `hermes-training` process
- Execution + maker-checker need persistent `hermes-trading-engine` API
- Cloud cycle **observes and persists** — it does not replace VPS execution

### MEMORY.md in git vs on VPS

| Location | Purpose |
|----------|---------|
| Repo `MEMORY.md` | GitHub Actions cloud state — committed each cycle |
| `{HTE_DATA_DIR}/MEMORY.md` | VPS Osmani loop memory — Docker volume |

Both follow SKILL_ANALYSIS §4 (disk-bound, no in-memory-only handoff).
