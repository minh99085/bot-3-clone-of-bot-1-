# Hermes RSI Divergence Indicator — paste into Pine Editor

Your file **`RSI Divergence Indicator.txt`** is the standard TradingView RSI divergence script.
It only has `alertcondition()` (English messages) — **the bot cannot read those**.

Use **`Hermes_RSI_Divergence_Indicator_Webhook.pine`** instead: same RSI math and chart labels,
plus JSON `alert()` webhooks the bot accepts.

**Pine CE10080:** If you see “Cannot include a `timeframe` argument in `indicator()` when the code
produces side effects”, remove `timeframe` / `timeframe_gaps` from the `indicator()` line — the
repo copy already omits them so `alert()` works.

## Quick setup (BTC + ETH 5m)

1. Open TradingView → Pine Editor → paste full contents of  
   `tradingview/Hermes_RSI_Divergence_Indicator_Webhook.pine`
2. Add to chart: `BINANCE:BTCUSDT` 5m and `BINANCE:ETHUSDT` 5m
3. Indicator **Inputs**:
   - **Hermes webhook secret** = VPS `TRADINGVIEW_WEBHOOK_SECRET`
   - **Webhook regular bull/bear only** = ON (default; hidden = plot only)
4. Create alert **with RSI indicator selected**:
   - Condition: **Any `alert()` function call**
   - Message: `{{message}}`
   - Webhook URL: `http://144.202.122.120/webhooks/tradingview`
5. Re-save alert after changing the secret input

## Read secret from VPS

```bash
ssh root@144.202.122.120 "grep ^TRADINGVIEW_WEBHOOK_SECRET= /opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine/.env | cut -d= -f2-"
```

## What the bot stores

| Field | Value |
|-------|--------|
| `signal_kind` | `rsi_divergence` |
| `signal_level` | `REGULAR_BULL_DIV` / `REGULAR_BEAR_DIV` |
| `indicator_name` | `Hermes RSI Divergence Indicator` |
| FIFO | last 20 per symbol (`BTCUSDT` / `ETHUSDT`) |

Hidden divergences still plot if enabled in inputs but **do not webhook** (bot overlay ignores hidden).

## Verify delivery

```bash
curl -s http://144.202.122.120/api/health
# then check status JSON on VPS:
ssh root@144.202.122.120 "docker exec hermes-trading-engine python -c \"
import json; from pathlib import Path
tv=json.loads(Path('/data/btc_pulse_status.json').read_text())['tradingview']
print('rsi', (tv.get('tradingview_alert_history') or {}).get('rsi_divergence_by_symbol'))
\""
```

You should see fresh `BTCUSDT` / `ETHUSDT` rows with `Hermes RSI Divergence Indicator`.
