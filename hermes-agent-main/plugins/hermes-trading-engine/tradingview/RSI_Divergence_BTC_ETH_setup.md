# RSI Divergence Indicator — BTC + ETH multi-TF webhook setup

## Charts (12 per asset = 24 alerts total)

| # | Symbol | Timeframe |
|---|--------|-----------|
| 1–12 | `INDEX:BTCUSD` or `BTCUSD` | 5m, 10m, 15m, 20m, 25m, 30m, 35m, 40m, 45m, 50m, 55m, **1h** |
| 1–12 | `INDEX:ETHUSD` or `ETHUSD` | same |

Script: **`RSI_Divergence_Indicator_Hermes_Webhook.pine`** (repo copy — do **not** use TradingView’s
built-in “RSI Divergence Indicator” from the public library). The built-in script only exposes
`alertcondition()` (plain English messages, no JSON, no secret). The Hermes copy adds `alert()` JSON
webhooks at bar close without changing RSI math or pivot logic.

## Webhook secret (required on every chart)

Paste the VPS secret into indicator **Inputs → Hermes webhook secret** on all 24 charts. It must
match `TRADINGVIEW_WEBHOOK_SECRET` in the plugin `.env` exactly (wrong/missing secret → bot returns
`401 bad_secret`; TradingView may still show the alert as “triggered”).

```bash
# read from VPS (operator)
ssh root@144.202.122.120 "grep ^TRADINGVIEW_WEBHOOK_SECRET= /opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine/.env | cut -d= -f2-"
```

After pasting the secret on a chart, re-save the alert (TradingView does not auto-refresh indicator
inputs on existing alerts).

## Indicator inputs (each chart)

| Input | Value |
|-------|-------|
| **Hermes webhook secret** | VPS `TRADINGVIEW_WEBHOOK_SECRET` (see command above) |
| **Hermes webhook URL** | `http://144.202.122.120/webhooks/tradingview` (reference only — also set in Alert) |
| **Bot name** | `hermes` (default) |

## Alert (each chart — one alert per chart)

| Field | Required value | Wrong value symptom |
|-------|----------------|---------------------|
| **Condition** | **Any `alert()` function call** | `Regular Bullish Divergence` / other `alertcondition` names send English text, not JSON → bot rejects |
| **Frequency** | **Once per bar close** | `Once per bar` can fire mid-candle then cancel when the pivot/divergence condition vanishes |
| **Webhook URL** | `http://144.202.122.120/webhooks/tradingview` | |
| **Message** | `{{message}}` | Fixed text breaks JSON parsing |

## Why many alerts do not fire (expected vs misconfiguration)

**VPS delivery is healthy** when `tradingview_alerts_rejected` stays at 0 and logs show
`tradingview alert ACCEPTED`. Selective firing is almost always on the TradingView / script side:

1. **Sparse by design** — RSI divergence fires only when a confirmed pivot + range filter
   (`5–60` bars since prior pivot) + price/RSI divergence align. Most bars have **no** signal.
   This is not a heartbeat indicator; quiet charts are normal.

2. **Pivot lag (`Pivot Lookback Right = 5`)** — labels appear 5 bars after the pivot forms; the
   `alert()` fires on the **confirmation bar close**, not when you first see the label drawing in
   real time. Divergences that look “obvious” intrabar often fail at bar close.

3. **Repainting appearance** — `ta.pivotlow` / `ta.pivothigh` need `lbR` bars to the right before
   `plFound` / `phFound` is true. Mid-candle marks can flicker; only the bar-close evaluation
   (`barstate.isconfirmed` in the Hermes wrapper) counts for webhooks.

4. **Wrong alert condition** — alerts bound to `alertcondition(...)` titles never call the Hermes
   `alert()` JSON builder. Use **one** alert per chart on **Any `alert()` function call**.

5. **Hidden divergence toggles** — `Plot Hidden Bullish/Bearish` default OFF hides shapes but Hermes
   `alert()` still emits hidden signals when enabled in script; regular bull/bear are the main lane.

6. **TradingView account limits** — expired alerts, alert quota, or paused charts stop delivery.
   Check Alert log → “Webhook sent” vs “Webhook failed” per trigger.

### Quick verification

```bash
# bot-side: last accepts / rejects
curl -s http://144.202.122.120/api/polymarket/training/btc_pulse | python3 -c "
import sys,json; tv=json.load(sys.stdin).get('tradingview',{})
print('received',tv.get('tradingview_alerts_received'),'valid',tv.get('tradingview_alerts_valid'),'rejected',tv.get('tradingview_alerts_rejected'))
print('reject_reasons',tv.get('tradingview_reject_reasons'))
"

# VPS logs (recent ACCEPTED / REJECTED)
ssh root@144.202.122.120 "docker logs hermes-training 2>&1 | grep tradingview | tail -20"
```

## How the bot uses alerts (observe-only — not trade gates)

| Phase | Hourly window | Bot consumer |
|-------|---------------|--------------|
| **Pre-trade** | 0–15m (`pre_band`) | `tv_2h_review.open_regime` — price-path trend context |
| **Entry** | 15–45m (`in_band`) | Pre-trade `actionable_trend` + Osmani triage TV align |
| **Post-trade** | 45–60m (`post_band`) | Council grades each `tv_<tf>m` member vs outcomes |

**Per-TF ladder:** `tv_5m` … `tv_60m` — each chart is an independent council member (graded follow/fade).

**Grok bundle:** `tradingview_per_tf_ladder`, `tradingview_2h_review.by_timeframe`, `tradingview_alert_history`.

**Pre-trade score:** `tv_ladder_alignment` + `tv_2h_alignment` components (restrict-only sizing).
