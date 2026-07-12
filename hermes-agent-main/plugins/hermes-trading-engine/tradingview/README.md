# TradingView — BTCUSD lite MTF (3 charts)

Polymarket BTC windows settle on **Chainlink BTC/USD**. Use **BTCUSD** or **INDEX:BTCUSD** on TradingView.

## Recommended script

**`Hermes_BTC_Pulse_Lite.pine`** — simple EMA + RSI, lightweight webhook JSON (direction, strength, signal_level per chart). Replaces the heavy v7 composite for observe-only / Grok MTF.

Legacy: `Hermes_BTC_Pulse_v7_IndexTrend.pine` (full composite v6 fields).

## Setup — minimum **3 alerts**

| # | Chart | Timeframe | Alert |
|---|-------|-----------|-------|
| 1 | BTCUSD | **2m** | Any `alert()` function call → webhook |
| 2 | BTCUSD | **3m** | same |
| 3 | BTCUSD | **4m** | same |

Webhook URL (all three):

```
http://<vps-ip>/webhooks/tradingview
```

Paste your VPS `TRADINGVIEW_WEBHOOK_SECRET` into the indicator **Hermes webhook secret** input on each chart.

The **Event ID suffix** input (default `bot1`) tags every alert's `event_id` so IDs stay unique; leave it at the default for this standalone bot.

### Indicator toggles (recommended)

| Setting | Recommended | Why |
|---------|-------------|-----|
| Send weak signals | ON | Early trend rows for Grok / learning |
| Send strong signals | ON | High-confidence `UP_STRONG` / `DOWN_STRONG` |
| Send FLAT on quiet bars | ON | Every bar close sends `FLAT` when no UP/DOWN — keeps `tf_2m/3m/4m_age_s` fresh |

Turn **FLAT heartbeat** off only if webhook volume is too high; MTF ages may go stale on quiet charts.

Bot env (already set via `scripts/apply-loop-arch-env.py`):

```
PULSE_TV_FEATURE_SYMBOL=BTCUSD
TRADINGVIEW_ALLOWED_SYMBOLS=BTCUSD,INDEX:BTCUSD,BTC/USD,BTC,XBTUSD
PULSE_TV_MTF_TIMEFRAMES=2,3,4
TRADINGVIEW_MAX_AGE_S=180
```

## Why 3 alerts (not 1 or 5)

- **1 alert** — only one timeframe; MTF stays `single_tf`, Grok never sees 2m+3m+4m alignment.
- **3 alerts** — matches bot `PULSE_TV_MTF_TIMEFRAMES=2,3,4`; each TF fires on its own bar close (~every 2–4 min). Fresh windows: 2m=300s, 3m=450s, 4m=600s.
- **More than 3** — only needed if you change `PULSE_TV_MTF_TIMEFRAMES` in `.env`.

## Lite JSON fields (per alert)

```json
{
  "secret": "...",
  "bot_name": "hermes",
  "symbol": "BTCUSD",
  "timeframe": "2",
  "direction": "UP",
  "signal_level": "UP_STRONG",
  "strength": 0.72,
  "event_id": "BTCUSD-2-...-UP_STRONG-lite-1",
  "bar_time": "...",
  "price": 60100.0
}
```

Quiet bar (heartbeat):

```json
{
  "timeframe": "3",
  "direction": "FLAT",
  "signal_level": "FLAT",
  "strength": 0.0
}
```

`FLAT` updates per-TF age and tells Grok “no trend on this chart” vs “chart silent.” Directional MTF count (`trend_fresh_count`) still only counts UP/DOWN.

Apply env after changes:

```bash
python3 /opt/Bot-1/scripts/apply-loop-arch-env.py
cd /opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine
docker compose up -d --force-recreate hermes-training
```