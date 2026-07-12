# RSI Divergence — TradingView official reference (bot primer source)

Bot/Grok read this via `tradingview_rsi_divergence.primer` and
`tradingview_alert_interpretation.guide` in every decision bundle.

## Official TradingView RSI documentation

**URL:** https://www.tradingview.com/support/solutions/43000502338-relative-strength-index-rsi/

### Wilder (1978) basics

- RSI = momentum oscillator on scale **0–100** (default length **14**, source **close**).
- **>70** overbought (Wilder) · **<30** oversold · **30–70** neutral · **~50** no trend.
- Built-in RSI has **Calculate Divergence** input — highlights bullish/bearish divergence on chart.
- Alerts for divergence only fire when **Calculate Divergence** is enabled in indicator settings.

### Wilder divergence (reversal signal)

| Type | Price | RSI | Wilder read |
|------|-------|-----|-------------|
| **Bullish** | New **lower low** | **Higher low** | Buying opportunity |
| **Bearish** | New **higher high** | **Lower high** | Selling opportunity |

### Cardwell (trend context)

- Bullish divergence usually only in **bearish trends**; bearish only in **bullish trends**.
- Divergence often = **brief correction**, not full trend reversal — use for trend **confirmation**.
- **Positive reversal** (bullish trend only): price higher low + RSI lower low → price rises.
- **Negative reversal** (bearish trend only): price lower high + RSI higher high → price falls.

### Failure swings (RSI-only, not pivot divergence)

**Bullish:** RSI <30 → bounce >30 → pullback stays >30 → breaks prior RSI high.

**Bearish:** RSI >70 → drop <70 → bounce stays <70 → breaks prior RSI low.

### TradingView divergence indicator limitations

**URL:** https://www.tradingview.com/support/solutions/43000589127-rsi-divergence-indicator/

- Divergence is **lagging** and not always present at reversals.
- Combine with other indicators — **never trade divergence alone**.
- Positive divergence = price could rise; negative = price could fall.

---

## Lane chart routing (operator 2026-07-11)

| Window | Charts | Feed |
|--------|--------|------|
| 1h | BTCUSDT / ETHUSDT | Binance USDT |
| 15m | BTCUSD / ETHUSD | Chainlink INDEX USD |

Never cross-feed: 1h reads *USDT FIFO only; 15m reads INDEX *USD FIFO only.

---

## Operator indicator (Hermes Pine)

Base: `docs/RSI Divergence Indicator.txt` → `Hermes_RSI_Divergence_Indicator_Webhook.pine`

| Setting | Default |
|---------|---------|
| RSI period | 14 |
| RSI source | close |
| Pivot lookback L/R | 5 / 5 |
| Pivot range | 5–60 bars |
| OB/OS lines | 70 / 30 |

### Four divergence types (Pine logic)

| Type | Price pivot | RSI pivot | Webhook to bot |
|------|-------------|-----------|----------------|
| Regular Bull | Lower low | Higher low | Yes (`REGULAR_BULL_DIV`) |
| Regular Bear | Higher high | Lower high | Yes (`REGULAR_BEAR_DIV`) |
| Hidden Bull | Higher low | Lower low | No (plot only) |
| Hidden Bear | Lower high | Higher high | No (plot only) |

### Bot usage (decision hierarchy)

1. **bar_close_5m** — PRIMARY price pattern (`short_path` lean drives entry side).
2. **rsi_band** — every-bar 30/70 zone (mean-revert backdrop + cross timing).
3. **rsi_divergence** — sparse pivot events + primer (confirm/fade overlay).
4. **tradingview_alert_interpretation** — synthesized `composite_lean` + `signal_agreement`.
5. **tv_rsi_overlay** — confirm/fade sizing only; never overrides price path.
