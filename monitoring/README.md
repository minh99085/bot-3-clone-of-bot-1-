# Bot monitoring over time

Track **design intent** + **live technical state** as the bot evolves.

## Three layers

| Layer | What | Where |
|-------|------|--------|
| **Design** | Architecture, trade authority, locked TV rules | `monitoring/design-manifest.json` |
| **Timeline** | Compact snapshot every pull (~hourly) | `monitoring/timeline.jsonl` |
| **Grades** | Technical + report composite scores | `monitoring/technical-grades.json`, `TECHNICAL_GRADES.md` |
| **Human report** | Plain-English operator summary | `monitoring/TECHNICAL_REPORT.md` |
| **Full artifacts** | Complete status/ledger/reports | `vps_full_reports/latest/` |

VPS also keeps `btc_pulse_score_history.json` (graded scores over time).

## Record a snapshot

```powershell
cd C:\Users\tieut\Bot-1

# After babysit pull (automatic if using pull script)
python scripts\pulse-babysit\record-timeline.py --from-latest
python scripts\pulse-babysit\grade-technical.py

# Standalone (hits live API)
python scripts\pulse-babysit\record-timeline.py

# Archive full JSON copy when config changes
.\scripts\pulse-babysit\pull-vps-artifacts.ps1
python scripts\pulse-babysit\record-timeline.py --from-latest --archive
```

## View trends

```powershell
# Last 24 hourly rows
python scripts\pulse-babysit\timeline-view.py

# What changed since last snapshot
python scripts\pulse-babysit\timeline-view.py --diff

# Last snapshot JSON
python scripts\pulse-babysit\timeline-view.py --json
```

## Automated (already set up)

- **GrokBot1-PulseBabysit** scheduled task → hourly `/pulse-babysit cycle`
- Each cycle: pull artifacts → evaluate → `record-timeline` (via pull script)
- **Dashboard**: http://144.202.122.120/ (live only, no history)

## What each timeline row captures

- `design`: series, TTC band, tick, max price, green path, cohort flags
- `tv`: alert counts, MTF verdict, per-TF direction/strength/age
- `oracle`: BTC price, RTDS health
- `ledger`: trades, WR, PF, PnL, open positions
- `funnel`: top gate rejections, cohort session blocks
- `recent_evals`: why last windows did not trade
- `config_fingerprint` + `config_changed`: detect env/deploy drift

## When you change bot design

1. Edit `scripts/apply-loop-arch-env.py` (env) or plugin code
2. Deploy `.\scripts\sync-vps.ps1`
3. Update `monitoring/design-manifest.json` if architecture changed
4. Next timeline row will show `config_changed: true`

## Git history

Commit `monitoring/timeline.jsonl` with babysit reports so you can diff weeks of operation in GitHub.