# 4-chart setup — BTC/ETH × BarClose5m + RSI5m Accurate

Only **4 charts / 4 alerts**. No 15m BarClose required.

| # | Symbol | TF | Script | Role |
|---|--------|----|--------|------|
| 1 | `BINANCE:BTCUSDT` | **5m** | `Hermes_BarClose_5m_Webhook.pine` | Path (last 50) |
| 2 | `BINANCE:ETHUSDT` | **5m** | `Hermes_BarClose_5m_Webhook.pine` | Path (last 50) |
| 3 | `BINANCE:BTCUSDT` | **5m** | `Hermes_RSI_Divergence_Indicator_Webhook.pine` | Overlay (last 20) |
| 4 | `BINANCE:ETHUSDT` | **5m** | `Hermes_RSI_Divergence_Indicator_Webhook.pine` | Overlay (last 20) |

**RSI script:** use `Hermes_RSI_Divergence_Indicator_Webhook.pine` — your standard
`RSI Divergence Indicator.txt` math + Hermes JSON `alert()`. Alternative:
`Hermes_RSI_Divergence_5m_Accurate.pine` (extra zone/delta filters).

Charts 1+3 can share one BTC 5m layout (BarClose overlay + RSI pane). Same for ETH.

## Alert (each of 4)

| Field | Value |
|-------|--------|
| Condition | **Any `alert()` function call** |
| Frequency UI | **Not shown** — Pine sets `alert.freq_once_per_bar_close` |
| Interval | **5m** |
| Webhook URL | `http://144.202.122.120/webhooks/tradingview` |
| Message | `{{message}}` |
| Inputs → Hermes webhook secret | VPS `TRADINGVIEW_WEBHOOK_SECRET` |

## Bot behavior

- Bar-close FIFO (50) on **BTCUSDT/ETHUSDT** plots dual-horizon trend (short≈last 8 × 5m).
- Bot auto-prefers `*USDT` 5m path over legacy `*USD` 15m when both exist.
- RSI FIFO (20) is confirm/fade only — never mixed into the path plot.
- Silent RSI = no-op (does not block).
- Grok-MC + decider receive `price_pattern.short_path` (OHLC) to plot the move.

## RSI troubleshooting (alerts never fire)

The Pine **logic was correct** (standard pivot + divergence math). The old **strict**
defaults were the problem — they filtered out almost all 5m signals:

| Gate (old strict) | Effect |
|-------------------|--------|
| Pivot 7/7 + range 8–36 only | Fewer pivots qualify |
| Bull RSI ≤ 38 / Bear RSI ≥ 62 | Drops mid-zone divergences |
| Min RSI Δ ≥ 4 + price move ≥ 0.12% | Drops small but valid divs |

**v2 script** adds **Accuracy profile** (default `balanced`). Use `standard` to match base TV RSI div.

### Verify on chart before creating alert

1. Add indicator → see green/red **Bull** / **Bear** labels on RSI pane (sparse is normal).
2. Enable **Plot near-miss** temporarily — yellow/orange dots = divergence ok but zone/delta blocked.
3. Create alert **with RSI indicator selected** (not BarClose):
   - Condition = **Any `alert()` function call**
   - Message = `{{message}}`
4. Re-save alert after pasting webhook secret in indicator inputs.

### Bot-side check

VPS should show `rsi_divergence_by_symbol` entries for `BTCUSDT` / `ETHUSDT` with
`indicator_name` = `Hermes RSI Divergence 5m Accurate` (not old `RSI Divergence Indicator`).

## RSI note

Accurate RSI is **sparse** (zone + pivot gates). If `rsi_divergence_by_symbol` only shows
old `RSI Divergence Indicator` on BTCUSD/ETHUSD, recreate alerts 3–4 on the Accurate script
with Condition = **Any `alert()`**.
