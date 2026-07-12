# Babysit outer loop (cloud / Linux)

Scheduled evaluation + VPS report pull. Complements `bot-1_loop.yml` (MEMORY.md sync).

## Run locally (cloud agent / Linux)

```bash
chmod +x scripts/pulse-babysit/*.sh
export BOT1_VPS_SSH_KEY=~/.ssh/bot1_grok_temp
bash scripts/pulse-babysit/run-babysit-cycle.sh
```

## GitHub Actions

Workflow `.github/workflows/bot-1-babysit.yml` runs every 30 minutes:

1. `scan-health.py`
2. `pull-vps-artifacts.sh` → wipe + pull `vps_full_reports/latest/`
3. `evaluate-cycle.py`
4. `apply-wr-tune.py --apply` when band/WR issues (never during starvation)
5. Commit + push report + `state.json`

VPS deploy after env changes requires `BOT1_VPS_SSH_KEY` secret (optional step in workflow).

## Windows (operator machine)

```
.\scripts\pulse-babysit\install-scheduled-task.ps1 -IntervalHours 1
```

## What this does NOT do

- Does not place live orders (paper only)
- Does not re-enable TV trade gates
- Does not run large code refactors — use cloud agent `/pulse-babysit cycle` for code fixes
