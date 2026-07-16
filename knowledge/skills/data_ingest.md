# Data Ingest Skill

## Cadence
- **Every 15 min:** Gamma active markets → `data/parquet/gamma/`; optional CLOB books
- **Nightly:** HuggingFace `SII-WANGZJ/Polymarket_data` trades.parquet (best-effort) → `data/parquet/bulk/`; synthetic fallback if geo/HTTP blocked
- Resume via `.part` Range requests; rate-limit aware (httpx timeouts)

## Dual source
- Gamma API for market metadata
- CLOB `/book` + `/prices-history` when token/market ids known
- CEX history still from `connectors/cex_realtime.py` (Coinbase fallback when Binance 451)

## State
`data/parquet/ingest_state.json` — `last_15m`, `last_nightly`, cursors

## Failure mode
Never block the trading loop. Log warning, write synthetic placeholder for bootstrap/offline.

## Auto Log
