# Full Trading Report — last 3 hours (2026-07-19)

Pulled from VPS paper fleet (`207.246.96.45`). Window: settlements with `settled_at` ≥ `2026-07-19T14:50:57.314160+00:00`.

## Summary

| Metric | Value |
|--------|-------|
| Window settled | 31 |
| Window W/L | 14 / 17 |
| Window WR | 45.2% |
| Window PnL | $+895.46 |
| Fleet equity (lifetime) | $20,895.46 / $20,000 |
| Fleet lifetime PnL | $+895.46 |
| Commit at pull | `775b4f8` |

## Per lane (window)

| Lane | Settled | PnL | WR | Equity | Turns | Orders |
|------|---------|-----|----|--------|-------|--------|
| lane01_baseline | 4 | $-240.00 | 0.0% | $1,760.00 | 31 | 4 |
| lane02_chainlink | 3 | $+92.73 | 33.3% | $2,092.73 | 34 | 3 |
| lane03_favorite | 0 | $+0.00 | n/a | $2,000.00 | 34 | 0 |
| lane04_longshot | 4 | $+200.71 | 50.0% | $2,200.71 | 34 | 4 |
| lane05_late | 0 | $+0.00 | n/a | $2,000.00 | 34 | 0 |
| lane06_garch | 5 | $+135.40 | 40.0% | $2,135.40 | 34 | 5 |
| lane07_marketsigma | 0 | $+0.00 | n/a | $2,000.00 | 34 | 0 |
| lane08_legacy | 4 | $+151.01 | 50.0% | $2,151.01 | 34 | 4 |
| lane09_random | 7 | $+361.99 | 71.4% | $2,361.99 | 34 | 7 |
| lane10_depth | 4 | $+193.62 | 50.0% | $2,193.62 | 34 | 4 |

## Files

| File | Purpose |
|------|---------|
| `report.txt` | Human-readable summary |
| `report.json` | Full structured report |
| `fleet_paper.json` | Fleet + lane stats |
| `trades.json` | Settled trades in the last 3 hours window |

