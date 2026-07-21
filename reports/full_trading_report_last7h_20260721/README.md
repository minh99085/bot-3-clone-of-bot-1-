# Full Trading Report — last 7 hours (2026-07-21)

Pulled from VPS paper fleet (`207.246.96.45`). Window: settlements with `settled_at` ≥ `2026-07-21T06:09:15.763617+00:00`.

## Summary

| Metric | Value |
|--------|-------|
| Window settled | 24 |
| Window W/L | 8 / 16 |
| Window WR | 33.3% |
| Window PnL | $+542.69 |
| Fleet equity (lifetime) | $20,861.79 / $20,000 |
| Fleet lifetime PnL | $+861.79 |
| Commit at pull | `4019f5c` |

## Per lane (window)

| Lane | Settled | PnL | WR | Equity | Turns | Orders |
|------|---------|-----|----|--------|-------|--------|
| lane01_baseline | 0 | $+0.00 | n/a | $1,754.31 | 84 | 0 |
| lane02_chainlink | 0 | $+0.00 | n/a | $1,683.01 | 84 | 0 |
| lane03_favorite | 0 | $+0.00 | n/a | $2,000.00 | 84 | 0 |
| lane04_longshot | 5 | $+164.39 | 40.0% | $2,285.10 | 81 | 5 |
| lane05_late | 2 | $+160.00 | 50.0% | $2,110.28 | 84 | 2 |
| lane06_garch | 3 | $-120.00 | 0.0% | $2,137.70 | 83 | 2 |
| lane07_marketsigma | 4 | $+190.48 | 50.0% | $2,259.10 | 84 | 4 |
| lane08_legacy | 5 | $-26.09 | 20.0% | $2,044.92 | 81 | 5 |
| lane09_random | 0 | $+0.00 | n/a | $2,299.84 | 0 | 0 |
| lane10_depth | 5 | $+173.91 | 40.0% | $2,287.53 | 81 | 5 |

## Files

| File | Purpose |
|------|---------|
| `report.txt` | Human-readable summary |
| `report.json` | Full structured report |
| `fleet_paper.json` | Fleet + lane stats |
| `trades.json` | Settled trades in the last 7 hours window |

