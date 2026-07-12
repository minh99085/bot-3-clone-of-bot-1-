# Monte Carlo Profit Discovery — BTC/ETH Polymarket

_Prepared 2026-07-11 04:48:51 UTC · PAPER ONLY · 1,000,000 discovery paths + honest overlay_

## 1. Bot data feed (what went into MC)

| Field | BTC | ETH |
|-------|----:|----:|
| Spot | 64160.48 | 1796.05 |
| sigma/s (live) | 1.729762e-05 | 5.482592e-05 |
| vol samples | 2680 | 2910 |

### Lane-routed TV charts

- **btc_1h** `BTCUSDT`: short_lean=up streak=1down bars=23 rsi_zone=neutral
- **eth_1h** `ETHUSDT`: short_lean=up streak=1down bars=23 rsi_zone=neutral
- **btc_15m** `BTCUSD`: short_lean=up streak=1down bars=12 rsi_zone=neutral
- **eth_15m** `ETHUSD`: short_lean=None streak=1down bars=13 rsi_zone=neutral

### Also fed
- TV signal learning: 96 settled grades
- Keep levels: ['DOWN_WEAK', 'UP_STRONG', 'UP_WEAK']
- Avoid levels: ['BAR_BEAR', 'BAR_BULL', 'DOWN_STRONG', 'FLAT']
- Council tv_2h_trend accuracy: 0.6069
- Calibration base_rate_up: 0.505 brier=0.263295
- Size $5 · slippage 1¢ · min entry 0.50

## 2. Discovery run

- **Paths:** 1,000,000 across BTC/ETH × 15m/1h × lean modes
- **Policies ranked:** 11,760
- **Runtime:** 32.23s

### Critical caveat (read first)

Top policies that buy at **ask=0.50 when model p≥0.60** show WR~94% and EV~$4. That is **selection on already-moved paths**, not free money — real Polymarket asks rise with p. Use those rows only as “when you have edge vs book.”

The **honest alpha test** below prices the market at a flat **0.55 ask** (near fair mid) and injects TV historical edge into path drift. That is the profit-discovery signal.

## 3. Honest TV alpha vs fair ask 0.55 (+1¢ slip)

| Signal | Hist edge vs 50% | Sim WR | EV $/trade | Verdict |
|--------|-----------------:|-------:|-----------:|---------|
| UP_WEAK | +0.136 | 0.576 | 0.1467 | **USE** |
| DOWN_WEAK | +0.000 | 0.497 | -0.5588 | AVOID |
| UP_STRONG | -0.019 | 0.488 | -0.6384 | AVOID |
| BAR_BULL | -0.115 | 0.434 | -1.1265 | AVOID |
| DOWN_STRONG | -0.167 | 0.406 | -1.3752 | AVOID |

**Only `UP_WEAK` is positive EV at fair pricing.** All other graded signal levels lose after slippage.

## 4. Gate structure (from 1M-path sweep)

| Gate | Aggregate WR | EV $/trade |
|------|-------------:|-----------:|
| `edge_ge_0.08` | 0.8672 | 2.4589 |
| `edge_ge_0.05` | 0.8529 | 2.3288 |
| `edge_ge_0.02` | 0.8384 | 2.1974 |
| `p_ge_0.60` | 0.8404 | 2.1382 |
| `p_ge_0.55` | 0.8153 | 1.9252 |
| `always` | 0.5 | -0.7532 |

- `always` (no edge filter): **EV −$0.75** → matches live bot bleed
- `edge_ge_0.05` / `edge_ge_0.08`: required for positive EV *if* book is mispriced vs model

## 5. Lean mode ranking

- `follow_tv2h_edge`: WR=0.7299 EV=$1.2313
- `follow_short`: WR=0.7283 EV=$1.218
- `fade_short`: WR=0.7281 EV=$1.2167
- `neutral`: WR=0.7278 EV=$1.2142
- `fade_streak3`: WR=0.7277 EV=$1.2135

Best lean mode: **`follow_tv2h_edge`** (uses council ~60% tv_2h accuracy).

## 6. Best-fit config for profit discovery

| Knob | Best fit |
|------|----------|
| Symbols 1h | `BTCUSDT` / `ETHUSDT` |
| Symbols 15m | `BTCUSD` / `ETHUSD` (INDEX) |
| Primary TV | `tv_2h_trend` + lean mode `follow_tv2h_edge` |
| Alert to trade | **`UP_WEAK` only** (honest +EV) |
| Alerts to block | `DOWN_STRONG`, `BAR_BULL`, `FLAT`, `UP_STRONG` at fair mid |
| BarClose 5m | Plot + fade after 3-bar streak; not sole entry |
| Entry gate | Require model−ask edge ≥ **0.05–0.08**; never `always` |
| Ask band | Prefer 0.50–0.60 when edge clears; avoid paying > model p |
| TTC | 1h: ~300s left; 15m: ~120–180s left (when edge exists) |
| Size | $5 paper; do not scale until live WR≥55% n≥100 in this cohort |

## 7. Files

- `mc_feed.json` — full bot state feed
- `mc_profit_discovery_result.json` — 1M-path sweep
- `mc_honest_overlay.json` — fair-market + TV-alpha tests
