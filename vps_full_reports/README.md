# VPS Full Reports

Live snapshots of the **BTC 5-minute pulse** PAPER engine running on the VPS.

**Canonical URL (only publish location):**
https://github.com/minh99085/Bot-1/tree/main/vps_full_reports/latest

> **Agent convention:** Read `.grok/rules/vps-full-report.md`. On every pull: wipe `latest/`,
> pull the **real full report** from VPS (engine writes `FULL_REPORT.md` every tick), remove stale
> tracked files, commit + push **only** the fresh snapshot to `origin/main`.

## `latest/` manifest

| File | Purpose |
|------|---------|
| `FULL_REPORT.md` | **Primary** report — dep-arb, P-UP, calibration, Kelly, trades, oracle, P&L |
| `report.md` | Short human-readable summary |
| `report.docx` | Word export |
| `LESSONS.md` | Operator lessons |
| `STATE.md` | Engine state snapshot |
| `MANIFEST.txt` | Artifact manifest |
| `validation_full.txt` / `validation_light.txt` | Validation output |
| `btc_pulse_meta_bundle.json` | Meta bundle |
| `btc_pulse_status.json` | Full engine status (oracle, ledger stats, calibration, overlay) |
| `btc_pulse_ledger.json` | Full paper ledger |
| `btc_pulse_light_report.json` | Light report JSON |
| `btc_pulse_tradingview.json` | TradingView observe-only feed |
| `btc_pulse_score_history.json` | Score history |
| `REPORT_EPOCH.json` | **Report baseline** — trading metrics in pulled reports count only from this UTC timestamp |
| `CYCLE_SUMMARY.md` | Plain-English operator summary (generated after pull) |

PAPER ONLY — no real orders. Oracle = Chainlink Data Streams reference price via Polymarket
RTDS `crypto_prices_chainlink` (`btc/usd`); Binance/Coinbase are lead predictors only;
settlement = official Polymarket resolution first, then RTDS Chainlink open/close proxy.