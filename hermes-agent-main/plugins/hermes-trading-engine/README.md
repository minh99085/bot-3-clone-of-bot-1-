# Hermes Trading Engine — BTC 5-min Pulse (paper)

A focused **paper-trading** engine for Polymarket's `btc-up-or-down-5m` series. It prices each
rolling 5-minute "Bitcoin Up or Down" window as a digital option and paper-trades the edge
versus the live CLOB book. **Simulated only — it never places a real order, holds a wallet, or
signs anything.**

## What it does

The contract resolves `Up` iff `Chainlink_BTC_close >= Chainlink_BTC_open` over the window
(ties → Up). Each tick the engine:

1. **Ingests** the current/upcoming windows from Polymarket Gamma.
2. **Snapshots** the window-open BTC price on a low-latency Coinbase proxy (open + live read on
   the same feed, so the Coinbase↔Chainlink basis cancels in the close-vs-open comparison).
3. **Prices** the window as a digital option:
   `P(up) = Φ( (ln(S_now/S_open) + (μ − 0.5σ²)·r) / (σ·√r) )`.
4. **Paper-trades** the side with the larger positive after-cost edge (loosened gates).
5. **Settles** against the authoritative Polymarket resolution and scores Brier calibration.

## Layout

```
engine/pulse/         the engine: markets, price, fair_value, strategy, executor, settlement, engine
engine/app.py         slim read-only API (health + pulse status/ledger) on :8800
scripts/run_btc_pulse.py   entrypoint (paper-only preflight)
tests/                test_btc_pulse_engine.py
```

## Run

```bash
docker compose up -d --build
docker compose logs -f hermes-training
curl http://localhost:8800/api/polymarket/training/btc_pulse
```

Tune the loosened gates via `PULSE_*` env in `docker-compose.yml`. Run the tests with
`python -m pytest tests/`.
