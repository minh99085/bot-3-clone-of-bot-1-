# Bot-3 — Full fleet paper report

- **Generated (UTC):** 2026-07-24T14:06:23.657161+00:00
- **Host:** `207.246.96.45` (`/opt/financial-freedom-bot`)
- **VPS git HEAD:** `9f47ec6`
- **Fleet:** 10 lanes · $2k each · $20k bankroll · BTC-15m + lane07 ETH-15m

## Fleet headline

| Metric | Value |
|--------|------:|
| Bankroll | $20,000 |
| Equity | $20,535.64 |
| **Fleet P&L** | **$+535.64** |
| Win rate | 65.9% |
| Settled trades | 126 |
| Wins / losses | 83 / 43 |
| Open positions | 1 |
| Lanes with data | 7 |

## Time windows (settlements)

| Window | n | W/L | WR | PnL |
|--------|--:|----:|---:|----:|
| all | 126 | 83/43 | 65.9% | $+535.64 |
| last_24h | 81 | 58/23 | 71.6% | $+663.55 |
| last_10h | 34 | 29/5 | 85.3% | $+622.44 |
| last_3h | 14 | 13/1 | 92.9% | $+261.67 |

## Per-lane scoreboard

| Lane | Asset | Filter | Variant | Role | Equity | PnL | Settled | W/L | WR | Status |
|------|-------|--------|---------|------|-------:|----:|--------:|----:|---:|--------|
| `lane01_baseline` | BTC | btc15 | baseline | control | $2,071.58 | $+71.58 | 12 | 7/5 | 58% | watching |
| `lane02_autonomy` | BTC | btc15 | autonomy | experiment | $2,129.88 | $+129.88 | 13 | 8/5 | 62% | watching |
| `lane03_drift` | BTC | btc15 | drift_barrier | experiment | $1,934.19 | $-65.81 | 23 | 14/9 | 61% | active |
| `lane04_favcont80` | BTC | btc15 | fav_cont_80 | experiment | $2,000.00 | $+0.00 | 0 | 0/0 | — | idle |
| `lane05_favsniper` | BTC | btc15 | fav_sniper | experiment | $2,000.00 | $+0.00 | 0 | 0/0 | — | active |
| `lane06_favlearn` | BTC | btc15 | fav_cont_70+learn | experiment | $2,051.18 | $+51.18 | 1 | 1/0 | 100% | watching |
| `lane07_ethdrift` | ETH | eth15 | drift_barrier | experiment | $2,000.00 | $+0.00 | 0 | 0/0 | — | watching |
| `lane08_favdepth` | BTC | btc15 | fav_cont_depth | experiment | $2,039.93 | $+39.93 | 4 | 4/0 | 100% | active |
| `lane09_random` | BTC | btc15 | random_null | null | $2,238.83 | $+238.83 | 61 | 38/23 | 62% | watching |
| `lane10_favopen` | BTC | btc15 | fav_cont_70 | experiment | $2,070.05 | $+70.05 | 12 | 11/1 | 92% | active |

## Paired scoreboard vs random null

- Null lane: `lane09_random`
- Shared BTC windows (paired): 0
- Note: **lane07 ETH** is unpaired vs BTC random by design.

| Lane | Asset | Role | n | WR | PnL | N paired | ΔPnL vs null |
|------|-------|------|--:|---:|----:|---------:|-------------:|
| `lane10_favopen` | BTC | experiment | 12 | 91.7% | $+70.05 | 4 | $+20.70 |
| `lane02_autonomy` | BTC | experiment | 13 | 61.5% | $+129.88 | 4 | $+9.31 |
| `lane09_random` | BTC | null | 61 | 62.3% | $+238.83 | 0 | $+0.00 |
| `lane06_favlearn` | BTC | experiment | 1 | 100.0% | $+51.18 | 0 | $+0.00 |
| `lane08_favdepth` | BTC | experiment | 4 | 100.0% | $+39.93 | 0 | $+0.00 |
| `lane04_favcont80` | BTC | experiment | 0 | 0.0% | $+0.00 | 0 | $+0.00 |
| `lane05_favsniper` | BTC | experiment | 0 | 0.0% | $+0.00 | 0 | $+0.00 |
| `lane07_ethdrift` | ETH | experiment | 0 | 0.0% | $+0.00 | 0 | $+0.00 |
| `lane03_drift` | BTC | experiment | 23 | 60.9% | $-65.81 | 7 | $-82.79 |
| `lane01_baseline` | BTC | control | 12 | 58.3% | $+71.58 | 6 | $-143.82 |
- lanes below 30 trades (noise): ['lane01_baseline', 'lane02_autonomy', 'lane03_drift', 'lane06_favlearn', 'lane08_favdepth', 'lane10_favopen']

## Ticket price buckets (all-time remaining ledger)

| Bucket | n | WR | PnL |
|--------|--:|---:|----:|
| Cheap ≤0.25 | 3 | 0.0% | $-64.29 |
| Mid | 88 | 58.0% | $+357.51 |
| Exp ≥0.75 | 35 | 91.4% | $+242.42 |

## Last 50 trades (newest first)

- Rows: 50 · Settled in view: 49 · Open in view: 1
- **Settled PnL in this table:** $+515.42
- **Fleet P&L (lifetime):** $+535.64

| # | Time UTC | Lane | Asset | Status | Dir | Size | Entry | Won | PnL | Slug |
|--:|----------|------|-------|--------|-----|-----:|------:|-----|----:|------|
| 1 | 2026-07-24T14:05:06.735276Z | `lane08_favdepth` | BTC | settled | DOWN | 40.00 | 0.796 | Y | +10.24 | `btc-updown-15m-1784900700` |
| 2 | 2026-07-24T14:05:06.674791Z | `lane03_drift` | BTC | settled | DOWN | 40.00 | 0.780 | Y | +11.28 | `btc-updown-15m-1784900700` |
| 3 | 2026-07-24T14:05:06.609072Z | `lane10_favopen` | BTC | settled | DOWN | 40.00 | 0.796 | Y | +10.24 | `btc-updown-15m-1784900700` |
| 4 | 2026-07-24T14:05:05.526678Z | `lane06_favlearn` | BTC | settled | DOWN | 200.00 | 0.796 | Y | +51.18 | `btc-updown-15m-1784900700` |
| 5 | 2026-07-24T14:00:05.170970Z | `lane03_drift` | BTC | open | UP | 40.00 | 0.550 | open | — | `btc-updown-15m-1784901600` |
| 6 | 2026-07-24T13:46:25.993274Z | `lane09_random` | BTC | settled | UP | 40.00 | 0.402 | Y | +59.55 | `btc-updown-15m-1784899800` |
| 7 | 2026-07-24T13:33:12.046883Z | `lane10_favopen` | BTC | settled | UP | 40.00 | 0.863 | Y | +6.38 | `btc-updown-15m-1784898900` |
| 8 | 2026-07-24T13:16:16.133392Z | `lane09_random` | BTC | settled | DOWN | 40.00 | 0.622 | Y | +24.29 | `btc-updown-15m-1784898000` |
| 9 | 2026-07-24T13:02:57.963606Z | `lane10_favopen` | BTC | settled | DOWN | 40.00 | 0.766 | Y | +12.20 | `btc-updown-15m-1784897100` |
| 10 | 2026-07-24T12:47:58.534390Z | `lane03_drift` | BTC | settled | UP | 40.00 | 0.603 | N | -40.00 | `btc-updown-15m-1784896200` |
| 11 | 2026-07-24T12:30:59.474158Z | `lane09_random` | BTC | settled | DOWN | 40.00 | 0.580 | Y | +28.97 | `btc-updown-15m-1784895300` |
| 12 | 2026-07-24T12:17:50.087316Z | `lane03_drift` | BTC | settled | DOWN | 40.00 | 0.760 | Y | +12.63 | `btc-updown-15m-1784894400` |
| 13 | 2026-07-24T12:02:39.194404Z | `lane08_favdepth` | BTC | settled | UP | 40.00 | 0.826 | Y | +8.41 | `btc-updown-15m-1784893500` |
| 14 | 2026-07-24T11:47:41.857234Z | `lane03_drift` | BTC | settled | DOWN | 40.00 | 0.760 | Y | +12.63 | `btc-updown-15m-1784892600` |
| 15 | 2026-07-24T11:15:38.611800Z | `lane09_random` | BTC | settled | UP | 40.00 | 0.427 | Y | +53.67 | `btc-updown-15m-1784890800` |
| 16 | 2026-07-24T11:02:23.704598Z | `lane01_baseline` | BTC | settled | DOWN | 40.00 | 0.370 | Y | +68.11 | `btc-updown-15m-1784889900` |
| 17 | 2026-07-24T10:45:28.712510Z | `lane09_random` | BTC | settled | UP | 40.00 | 0.546 | N | -40.00 | `btc-updown-15m-1784889000` |
| 18 | 2026-07-24T10:30:23.529102Z | `lane09_random` | BTC | settled | UP | 40.00 | 0.465 | Y | +46.05 | `btc-updown-15m-1784888100` |
| 19 | 2026-07-24T10:02:17.901991Z | `lane03_drift` | BTC | settled | UP | 40.00 | 0.760 | Y | +12.63 | `btc-updown-15m-1784886300` |
| 20 | 2026-07-24T10:02:15.779358Z | `lane10_favopen` | BTC | settled | UP | 40.00 | 0.756 | Y | +12.89 | `btc-updown-15m-1784886300` |
| 21 | 2026-07-24T10:02:14.293441Z | `lane02_autonomy` | BTC | settled | UP | 40.00 | 0.527 | Y | +35.90 | `btc-updown-15m-1784886300` |
| 22 | 2026-07-24T10:02:08.307110Z | `lane01_baseline` | BTC | settled | UP | 40.00 | 0.790 | Y | +10.63 | `btc-updown-15m-1784886300` |
| 23 | 2026-07-24T09:32:04.435049Z | `lane03_drift` | BTC | settled | DOWN | 40.00 | 0.507 | Y | +38.86 | `btc-updown-15m-1784884500` |
| 24 | 2026-07-24T09:32:01.029312Z | `lane02_autonomy` | BTC | settled | DOWN | 40.00 | 0.507 | Y | +38.86 | `btc-updown-15m-1784884500` |
| 25 | 2026-07-24T09:31:55.410677Z | `lane01_baseline` | BTC | settled | DOWN | 40.00 | 0.507 | Y | +38.86 | `btc-updown-15m-1784884500` |
| 26 | 2026-07-24T09:16:57.289890Z | `lane02_autonomy` | BTC | settled | DOWN | 40.00 | 0.497 | Y | +40.55 | `btc-updown-15m-1784883600` |
| 27 | 2026-07-24T09:16:51.547505Z | `lane01_baseline` | BTC | settled | DOWN | 40.00 | 0.590 | Y | +27.80 | `btc-updown-15m-1784883600` |
| 28 | 2026-07-24T08:34:40.134901Z | `lane09_random` | BTC | settled | DOWN | 40.00 | 0.507 | Y | +38.86 | `btc-updown-15m-1784880900` |
| 29 | 2026-07-24T08:19:31.583056Z | `lane09_random` | BTC | settled | UP | 40.00 | 0.517 | N | -40.00 | `btc-updown-15m-1784880000` |
| 30 | 2026-07-24T07:49:14.703762Z | `lane09_random` | BTC | settled | DOWN | 40.00 | 0.810 | Y | +9.38 | `btc-updown-15m-1784878200` |
| 31 | 2026-07-24T07:34:06.639331Z | `lane09_random` | BTC | settled | DOWN | 40.00 | 0.556 | Y | +31.97 | `btc-updown-15m-1784877300` |
| 32 | 2026-07-24T06:46:11.542139Z | `lane10_favopen` | BTC | settled | DOWN | 40.00 | 0.776 | N | -40.00 | `btc-updown-15m-1784874600` |
| 33 | 2026-07-24T06:46:11.234491Z | `lane02_autonomy` | BTC | settled | DOWN | 40.00 | 0.412 | N | -40.00 | `btc-updown-15m-1784874600` |
| 34 | 2026-07-24T06:01:02.219380Z | `lane03_drift` | BTC | settled | UP | 40.00 | 0.613 | Y | +25.28 | `btc-updown-15m-1784871900` |
| 35 | 2026-07-24T04:30:42.576657Z | `lane09_random` | BTC | settled | UP | 40.00 | 0.475 | Y | +44.14 | `btc-updown-15m-1784866500` |
| 36 | 2026-07-24T04:00:32.909030Z | `lane09_random` | BTC | settled | UP | 40.00 | 0.454 | Y | +48.05 | `btc-updown-15m-1784864700` |
| 37 | 2026-07-24T04:00:32.775174Z | `lane03_drift` | BTC | settled | UP | 40.00 | 0.454 | Y | +48.05 | `btc-updown-15m-1784864700` |
| 38 | 2026-07-24T03:45:26.906112Z | `lane03_drift` | BTC | settled | DOWN | 37.68 | 0.510 | N | -37.68 | `btc-updown-15m-1784863800` |
| 39 | 2026-07-24T03:30:21.283870Z | `lane09_random` | BTC | settled | DOWN | 40.00 | 0.546 | N | -40.00 | `btc-updown-15m-1784862900` |
| 40 | 2026-07-24T03:30:21.238514Z | `lane03_drift` | BTC | settled | DOWN | 40.00 | 0.420 | N | -40.00 | `btc-updown-15m-1784862900` |
| 41 | 2026-07-24T02:03:21.197439Z | `lane02_autonomy` | BTC | settled | UP | 40.00 | 0.603 | N | -40.00 | `btc-updown-15m-1784857500` |
| 42 | 2026-07-24T02:01:11.039220Z | `lane09_random` | BTC | settled | DOWN | 40.00 | 0.594 | Y | +27.36 | `btc-updown-15m-1784857500` |
| 43 | 2026-07-24T01:46:05.341228Z | `lane09_random` | BTC | settled | DOWN | 40.00 | 0.507 | N | -40.00 | `btc-updown-15m-1784856600` |
| 44 | 2026-07-24T01:18:04.484120Z | `lane02_autonomy` | BTC | settled | UP | 40.00 | 0.433 | Y | +52.33 | `btc-updown-15m-1784854800` |
| 45 | 2026-07-24T00:45:48.960647Z | `lane09_random` | BTC | settled | UP | 40.00 | 0.537 | N | -40.00 | `btc-updown-15m-1784853000` |
| 46 | 2026-07-24T00:32:48.339444Z | `lane02_autonomy` | BTC | settled | DOWN | 40.00 | 0.546 | Y | +33.23 | `btc-updown-15m-1784852100` |
| 47 | 2026-07-24T00:30:44.401691Z | `lane09_random` | BTC | settled | UP | 40.00 | 0.575 | N | -40.00 | `btc-updown-15m-1784852100` |
| 48 | 2026-07-24T00:02:35.837198Z | `lane02_autonomy` | BTC | settled | UP | 32.90 | 0.371 | N | -32.90 | `btc-updown-15m-1784850300` |
| 49 | 2026-07-23T23:20:02.604158Z | `lane01_baseline` | BTC | settled | UP | 40.00 | 0.517 | N | -40.00 | `btc-updown-15m-1784847600` |
| 50 | 2026-07-23T23:15:21.547496Z | `lane09_random` | BTC | settled | DOWN | 40.00 | 0.537 | Y | +34.54 | `btc-updown-15m-1784847600` |

