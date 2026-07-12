# Bot cycle summary (plain English)

_Updated: 2026-07-11 02:41 UTC_

_Trading report baseline: **2026-07-11 02:40:51 UTC** (token `2026-07-11-btc-eth-cleanup`) — metrics below are since this point._

## Last cycle

| | |
|---|---|
| **Cycle #** | 2 |
| **Checked at** | 2026-07-08 00:57 UTC |
| **Result** | **blocked** |
| **What it means** | Stopped — serious problem found. Check issues below. |
| **Next check after** | — |

**Issues flagged:** verifier_disabled, trade_starvation_streak

**Fixes applied:**

- mid_exit_convergence paper lane (60s horizon)
- wire dep_arb stop halt before new executes
- max_entry_vwap 0.52 + PULSE_MAX_PRICE 0.52
- stop guard recovery when mid_convergence n>=5 rate>=0.5
- evaluate-cycle strategy_halted names correct strategy

## How the bot is doing now

| | |
|---|---|
| **Mode** | Paper only (fake money) |
| **Started with** | $500.00 |
| **Total now** | $2,000.00 (0.0% return) |
| **Arb profit** | — (0 trades) |
| **Directional profit** | $0.00 |
| **Win rate** | — (0 settled trades) |
| **UP win rate** | — |
| **DOWN win rate** | — |
| **Bot stopped?** | No — bot is running |
| **Overall grade** | — (—/100) |

### 5m vs 15m (recent)

| Market | Trades | Win rate | PnL |
|--------|--------|----------|-----|
| **15m** | — | — | — |
| **5m** | — | — | — |

### TradingView (INDEX:BTCUSD)

- Alerts received: **0**
- 5-chart trend: **none** (—/12 fresh)

## Quick verdict

**Good:** Bot is running normally.

**Watch:** Few TradingView alerts so far.

---

_Auto-generated after each `/pulse-babysit` cycle. Full report: `report.md` / `report.docx` in this folder._
