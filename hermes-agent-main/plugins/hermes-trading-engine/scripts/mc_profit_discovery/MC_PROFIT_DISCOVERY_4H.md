# Monte Carlo Profit Discovery — Polymarket BTC/ETH 4h

_Prepared 2026-07-11 05:02:49 UTC · PAPER ONLY · 1,000,000 paths_

## Bot feed (same inputs as 15m/1h sim)

| Asset | Spot | sigma/s |
|-------|------:|--------:|
| BTC | 64095.185429634534 | 1.607178257819053e-05 |
| ETH | 1794.715 | 6.454246122272403e-05 |

- **btc_4h** `BTCUSDT`: regime_lean=up δ=0.01 short_1h=up bars=26
- **eth_4h** `ETHUSDT`: regime_lean=up δ=0.1036 short_1h=up bars=26

Council: tv_2h=0.604 tv_240m=0.25

## Honest TV alpha vs fair ask 0.55 (+1¢ slip) — THE EDGE TEST

| Signal | Hist edge | Sim WR | EV $/trade | Verdict |
|--------|----------:|-------:|-----------:|---------|
| TV240_FADE | +0.250 | 0.634 | 0.6607 | **USE** |
| UP_WEAK | +0.136 | 0.581 | 0.1904 | **USE** |
| TV2H_FOLLOW | +0.104 | 0.550 | -0.0868 | **AVOID** |
| DOWN_WEAK | +0.000 | 0.502 | -0.5190 | **AVOID** |
| BAR_BEAR | +0.000 | 0.496 | -0.5732 | **AVOID** |
| UP_STRONG | -0.018 | 0.493 | -0.5997 | **AVOID** |
| BAR_BULL | -0.115 | 0.438 | -1.0875 | **AVOID** |
| DOWN_STRONG | -0.167 | 0.413 | -1.3121 | **AVOID** |

## Gate summary (1M sweep)

- `edge_ge_0.08`: WR=0.9079 EV=$2.7715
- `edge_ge_0.05`: WR=0.897 EV=$2.6735
- `edge_ge_0.02`: WR=0.8858 EV=$2.5733
- `p_ge_0.60`: WR=0.8865 EV=$2.5297
- `p_ge_0.55`: WR=0.8668 EV=$2.3626
- `always`: WR=0.505 EV=$-0.7103

## Lean mode summary

- `up_weak_only`: WR=0.8056 EV=$1.8638
- `fade_tv240`: WR=0.7706 EV=$1.5649
- `follow_tv2h_edge`: WR=0.765 EV=$1.5195
- `follow_short_1h`: WR=0.7646 EV=$1.5161
- `fade_regime`: WR=0.7642 EV=$1.5133
- `follow_regime`: WR=0.7641 EV=$1.5119
- `fade_streak3`: WR=0.764 EV=$1.5117
- `neutral`: WR=0.7638 EV=$1.5098

## Top 10 sweep policies (caveat: need real book edge)

1. btc ttc=300 down ask=0.5 fade_tv240/p_ge_0.60 WR=0.9744 EV=$4.5529
2. eth ttc=300 down ask=0.5 fade_tv240/p_ge_0.60 WR=0.9739 EV=$4.5483
3. btc ttc=300 down ask=0.5 fade_tv240/edge_ge_0.08 WR=0.9727 EV=$4.5362
4. eth ttc=300 down ask=0.5 fade_tv240/edge_ge_0.08 WR=0.9723 EV=$4.532
5. btc ttc=300 up ask=0.5 up_weak_only/p_ge_0.60 WR=0.9709 EV=$4.5182
6. eth ttc=300 up ask=0.5 up_weak_only/p_ge_0.60 WR=0.9704 EV=$4.514
7. btc ttc=300 down ask=0.5 fade_tv240/edge_ge_0.05 WR=0.97 EV=$4.5098
8. btc ttc=300 down ask=0.5 fade_tv240/p_ge_0.55 WR=0.97 EV=$4.5098
9. btc ttc=300 up ask=0.5 follow_tv2h_edge/p_ge_0.60 WR=0.9699 EV=$4.5085
10. eth ttc=300 up ask=0.5 follow_tv2h_edge/p_ge_0.60 WR=0.9698 EV=$4.5075

## Recommendation

```json
{
  "market": "btc/eth-up-or-down-4h",
  "edge_found": true,
  "honest_use_signals": [
    {
      "signal": "TV240_FADE",
      "hist_edge": 0.25,
      "mean_wr": 0.634,
      "mean_ev": 0.6607,
      "n_paths": 25000,
      "verdict": "USE"
    },
    {
      "signal": "UP_WEAK",
      "hist_edge": 0.1364,
      "mean_wr": 0.5813,
      "mean_ev": 0.1904,
      "n_paths": 25000,
      "verdict": "USE"
    }
  ],
  "honest_avoid_signals": [
    {
      "signal": "TV2H_FOLLOW",
      "hist_edge": 0.104,
      "mean_wr": 0.5503,
      "mean_ev": -0.0868,
      "n_paths": 25000,
      "verdict": "AVOID"
    },
    {
      "signal": "DOWN_WEAK",
      "hist_edge": 0.0,
      "mean_wr": 0.5019,
      "mean_ev": -0.519,
      "n_paths": 25000,
      "verdict": "AVOID"
    },
    {
      "signal": "BAR_BEAR",
      "hist_edge": 0.0,
      "mean_wr": 0.4958,
      "mean_ev": -0.5732,
      "n_paths": 25000,
      "verdict": "AVOID"
    },
    {
      "signal": "UP_STRONG",
      "hist_edge": -0.0185,
      "mean_wr": 0.4929,
      "mean_ev": -0.5997,
      "n_paths": 25000,
      "verdict": "AVOID"
    },
    {
      "signal": "BAR_BULL",
      "hist_edge": -0.1154,
      "mean_wr": 0.4382,
      "mean_ev": -1.0875,
      "n_paths": 25000,
      "verdict": "AVOID"
    },
    {
      "signal": "DOWN_STRONG",
      "hist_edge": -0.1667,
      "mean_wr": 0.4131,
      "mean_ev": -1.3121,
      "n_paths": 25000,
      "verdict": "AVOID"
    }
  ],
  "best_sweep_policies": [
    {
      "key": "btc|4h|ttc300|down|ask0.50|fade_tv240|p_ge_0.60",
      "asset": "btc",
      "window": "4h",
      "ttc_s": 300,
      "side": "down",
      "ask": 0.5,
      "lean_mode": "fade_tv240",
      "gate": "p_ge_0.60",
      "symbol": "BTCUSDT",
      "sigma": 1.607178257819053e-05,
      "mu": -4.735194352830974e-08,
      "n_trades": 313749,
      "win_rate": 0.9744,
      "total_pnl_usd": 1428470.69,
      "ev_per_trade_usd": 4.5529
    },
    {
      "key": "eth|4h|ttc300|down|ask0.50|fade_tv240|p_ge_0.60",
      "asset": "eth",
      "window": "4h",
      "ttc_s": 300,
      "side": "down",
      "ask": 0.5,
      "lean_mode": "fade_tv240",
      "gate": "p_ge_0.60",
      "symbol": "ETHUSDT",
      "sigma": 6.454246122272403e-05,
      "mu": -1.901600500210748e-07,
      "n_trades": 313324,
      "win_rate": 0.9739,
      "total_pnl_usd": 1425085.88,
      "ev_per_trade_usd": 4.5483
    },
    {
      "key": "btc|4h|ttc300|down|ask0.50|fade_tv240|edge_ge_0.08",
      "asset": "btc",
      "window": "4h",
      "ttc_s": 300,
      "side": "down",
      "ask": 0.5,
      "lean_mode": "fade_tv240",
      "gate": "edge_ge_0.08",
      "symbol": "BTCUSDT",
      "sigma": 1.607178257819053e-05,
      "mu": -4.735194352830974e-08,
      "n_trades": 315181,
      "win_rate": 0.9727,
      "total_pnl_usd": 1429732.25,
      "ev_per_trade_usd": 4.5362
    },
    {
      "key": "eth|4h|ttc300|down|ask0.50|fade_tv240|edge_ge_0.08",
      "asset": "eth",
      "window": "4h",
      "ttc_s": 300,
      "side": "down",
      "ask": 0.5,
      "lean_mode": "fade_tv240",
      "gate": "edge_ge_0.08",
      "symbol": "ETHUSDT",
      "sigma": 6.454246122272403e-05,
      "mu": -1.901600500210748e-07,
      "n_trades": 314746,
      "win_rate": 0.9723,
      "total_pnl_usd": 1426417.06,
      "ev_per_trade_usd": 4.532
    },
    {
      "key": "btc|4h|ttc300|up|ask0.50|up_weak_only|p_ge_0.60",
      "asset": "btc",
      "window": "4h",
      "ttc_s": 300,
      "side": "up",
      "ask": 0.5,
      "lean_mode": "up_weak_only",
      "gate": "p_ge_0.60",
      "symbol": "BTCUSDT",
      "sigma": 1.607178257819053e-05,
      "mu": 2.57594572794005e-08,
      "n_trades": 281419,
      "win_rate": 0.9709,
      "total_pnl_usd": 1271503.04,
      "ev_per_trade_usd": 4.5182
    }
  ],
  "best_by_asset": {
    "btc": {
      "key": "btc|4h|ttc300|down|ask0.50|fade_tv240|p_ge_0.60",
      "asset": "btc",
      "window": "4h",
      "ttc_s": 300,
      "side": "down",
      "ask": 0.5,
      "lean_mode": "fade_tv240",
      "gate": "p_ge_0.60",
      "symbol": "BTCUSDT",
      "sigma": 1.607178257819053e-05,
      "mu": -4.735194352830974e-08,
      "n_trades": 313749,
      "win_rate": 0.9744,
      "total_pnl_usd": 1428470.69,
      "ev_per_trade_usd": 4.5529
    },
    "eth": {
      "key": "eth|4h|ttc300|down|ask0.50|fade_tv240|p_ge_0.60",
      "asset": "eth",
      "window": "4h",
      "ttc_s": 300,
      "side": "down",
      "ask": 0.5,
      "lean_mode": "fade_tv240",
      "gate": "p_ge_0.60",
      "symbol": "ETHUSDT",
      "sigma": 6.454246122272403e-05,
      "mu": -1.901600500210748e-07,
      "n_trades": 313324,
      "win_rate": 0.9739,
      "total_pnl_usd": 1425085.88,
      "ev_per_trade_usd": 4.5483
    }
  },
  "always_neutral_baseline": {
    "key": "eth|4h|ttc10800|down|ask0.50|neutral|always",
    "asset": "eth",
    "window": "4h",
    "ttc_s": 10800,
    "side": "down",
    "ask": 0.5,
    "lean_mode": "neutral",
    "gate": "always",
    "symbol": "ETHUSDT",
    "sigma": 6.454246122272403e-05,
    "mu": 0.0,
    "n_trades": 500000,
    "win_rate": 0.5031,
    "total_pnl_usd": -33725.49,
    "ev_per_trade_usd": -0.0675
  },
  "symbols": {
    "btc_4h": "BTCUSDT (lead) / BTCUSD (settle)",
    "eth_4h": "ETHUSDT (lead) / ETHUSD (settle)"
  },
  "note": "Honest edge = TV hist tilt vs fair ask 0.55. Sweep edge_ge_* rows need real book mispricing. tv_240m is anti-predictive \u2014 fade, don't follow."
}
```
