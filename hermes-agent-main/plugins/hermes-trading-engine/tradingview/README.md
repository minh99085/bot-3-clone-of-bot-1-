# TradingView — Bot 3 Directional (5m RSI Divergence only)

Polymarket BTC/ETH windows settle on **Chainlink index prices**. Use **INDEX:BTCUSD** and **INDEX:ETHUSD** on TradingView (not Binance perps).

## Script

**`Hermes_RSI_Divergence_Indicator_Webhook.pine`** — pivot-based RSI divergence with JSON webhook. Fires only on **regular** bull/bear divergence (no band heartbeat, no bar-close, no multi-TF).

## Setup — exactly **2 alerts**

| # | Chart | Timeframe | Script |
|---|-------|-----------|--------|
| 1 | `INDEX:BTCUSD` | **5m** | `Hermes_RSI_Divergence_Indicator_Webhook.pine` |
| 2 | `INDEX:ETHUSD` | **5m** | same |

### Alert settings (both charts)

| Field | Value |
|-------|-------|
| Condition | **Any alert() function call** |
| Message | `{{message}}` |
| Webhook URL | `http://207.246.96.45/webhooks/tradingview` |
| Frequency | Once per bar close (set by Pine) |

### Indicator inputs

| Input | Value |
|-------|-------|
| Hermes webhook secret | VPS `TRADINGVIEW_WEBHOOK_SECRET` |
| Hermes webhook URL | `http://207.246.96.45/webhooks/tradingview` |
| Event ID suffix | `bot3` |
| Webhook regular bull/bear only | **ON** |

Get the secret on VPS:

```bash
ssh root@207.246.96.45 "grep ^TRADINGVIEW_WEBHOOK_SECRET= /opt/Bot-3/hermes-agent-main/plugins/hermes-trading-engine/.env | cut -d= -f2-"
```

**Do not** create alerts from alertcondition titles (Regular Bullish Divergence, etc.) — those send English text, not JSON.

## What the bot learns

5m RSI divergence webhooks feed the **15m directional lane** as an observe-only overlay:

```
Pine (rsi_divergence) → tradingview.py rsi_div_history FIFO
  → tv_rsi_overlay (confirm/fade sizing on 15m entries)
  → tv_rsi_divergence (Grok teaching + analysis bundle)
  → lane_15m_learner (grades outcomes by RSI overlay alignment)
```

**Price trend** (rising/falling/flat) comes from **Chainlink spot** via `price_action_trend.py`, not from TradingView labels. RSI divergence is a separate confirm/fade signal layered on top.

## Bot env (set via `scripts/apply-loop-arch-env.py`)

```
PULSE_TV_MTF_TIMEFRAMES=5
TRADINGVIEW_ALLOWED_SYMBOLS=BTCUSD,INDEX:BTCUSD,ETHUSD,INDEX:ETHUSD
PULSE_TV_RSI_OVERLAY_ENABLED=1
PULSE_TV_RSI_DIVERGENCE_ANALYSIS_ENABLED=1
PULSE_TV_RSI_BAND_ENABLED=0
PULSE_TV_15M_CHART_LEAN_ENABLED=0
PULSE_TV_1H_CHART_LEAN_ENABLED=0
PULSE_TV_2H_REVIEW_ENABLED=0
PULSE_TRIAGE_TREND_SOURCE=price
```

Apply on VPS after deploy:

```bash
python3 /opt/Bot-3/scripts/setup-vps-training-env.py
cd /opt/Bot-3/hermes-agent-main/plugins/hermes-trading-engine
docker compose down --remove-orphans
docker compose build
docker compose up -d --force-recreate --remove-orphans
```

## Expected webhook JSON

```json
{
  "secret": "...",
  "symbol": "BTCUSD",
  "timeframe": "5",
  "direction": "UP",
  "signal_level": "REGULAR_BULL_DIV",
  "signal_kind": "rsi_divergence",
  "divergence_kind": "regular_bullish",
  "event_id": "BTCUSD-5-...-REGULAR_BULL_DIV-rsidiv-bot3",
  "observe_only": true
}
```

Verify intake on dashboard or:

```bash
ssh root@207.246.96.45 "tail -20 /opt/Bot-3/hermes-agent-main/plugins/hermes-trading-engine/data/btc_pulse_tradingview.json"
```
