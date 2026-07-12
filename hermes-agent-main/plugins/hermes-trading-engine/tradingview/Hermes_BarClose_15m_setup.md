# Hermes BarClose 15m — BTC + ETH webhook setup

## Indicator (final choice)

**`Hermes_BarClose_15m_Webhook.pine`** — OHLC **bar-close price action** (not RSI Divergence).

| Why this | Why not RSI Divergence |
|----------|-------------------------|
| Every 15m bar fires → dense path for Grok | Sparse pivots; 5-bar lag > 15m window |
| Continuation / displacement aligned to Polymarket | Reversal signal — wrong for open/close binaries |
| Horizon-matched to 15m lane (TTM 900s) | Ladder was built for hourly council grading |

Settlement truth stays **Chainlink** inside the bot. TV is **observe-only** lead/context on chart price (prefer `BINANCE:BTCUSDT` / `BINANCE:ETHUSDT` to match the rules index; `INDEX:BTCUSD` / `INDEX:ETHUSD` also allowed).

## Charts (2 alerts total)

| # | Symbol | Timeframe |
|---|--------|-----------|
| 1 | `BINANCE:BTCUSDT` or `INDEX:BTCUSD` | **15** |
| 2 | `BINANCE:ETHUSDT` or `INDEX:ETHUSD` | **15** |

## Bot storage (hard cap 50) + dual horizon

Engine keeps a **FIFO of 50 alerts per symbol**. New alert → append; when full → drop oldest.

| Horizon | Alerts | Role |
|---------|--------|------|
| **Regime** | last **50** (~12.5h) | HTF structure for Grok — context only |
| **Short-term** | last **6–8** (~1.5–2h) | Current trade lean — size bias on 15m lane |

Env (applied by `scripts/apply-loop-arch-env.py`):

```
PULSE_TV_ALERT_HISTORY_PER_SYMBOL=50
PULSE_TV_15M_SHORT_PATH_N=8
PULSE_TV_15M_CHART_LEAN_ENABLED=1
PULSE_TV_15M_CHART_LEAN_SIZE=1
PULSE_TV_2H_ALERT_HISTORY_CAP=50
```

Grok reads `tradingview_15m_price_path.focus.short_term` + `.regime`. Lane learner grades `tv_15m_lean_aligned` on settle.

## Webhook secret

Paste VPS `TRADINGVIEW_WEBHOOK_SECRET` into **Inputs → Hermes webhook secret** on both charts.

```bash
ssh root@144.202.122.120 "grep ^TRADINGVIEW_WEBHOOK_SECRET= /opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine/.env | cut -d=-f2-"
```

## Alert (each chart)

| Field | Required value |
|-------|----------------|
| **Condition** | **Any `alert()` function call** |
| **Frequency** | **Once per bar close** |
| **Webhook URL** | `http://144.202.122.120/webhooks/tradingview` |
| **Message** | `{{message}}` |

## Payload fields (Hermes JSON)

`direction` UP/DOWN · `signal_level` BAR_BULL/BAR_BEAR · `strength` body ratio · `price`/`open`/`high`/`low`/`close` · `body_pct` · `streak` · `signal_kind=bar_close_15m` · `event_id` ends with `-bar15m-bot1`

## Verify

```bash
curl -s http://144.202.122.120/api/polymarket/training/btc_pulse | python3 -c "
import sys,json
s=json.load(sys.stdin)
tv=s.get('tradingview') or {}
print('valid', tv.get('tradingview_alerts_valid'), 'hist_cap', tv.get('tradingview_alert_history_per_symbol'))
print('hist_counts', tv.get('tradingview_alert_history_counts'))
print('15m_path', (s.get('tradingview_15m_price_path') or {}) .get('note','(see grok bundle)'))
"
```
