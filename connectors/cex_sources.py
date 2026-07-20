"""Multi-exchange CEX price sources — no Binance (geo-blocked 451 on the VPS).

Health-aware rotation across Coinbase, Kraken, Bitstamp, OKX for:
  - live mids (bid/ask) per asset
  - 1-minute kline OPEN at a timestamp (barrier strike / settlement refs)

Design:
  * per-source circuit breaker: 3 consecutive failures → 120s cooldown, so a
    rate-limited or blocked venue stops being retried on every call (the
    Binance-first chain burned seconds per fetch and 429'd the fallback);
  * order configurable via HERMES_CEX_SOURCES (default
    "coinbase,kraken,bitstamp,okx");
  * `get_mid_multi` returns up to k independent venues for cross-venue
    agreement checks (replaces the old Binance-vs-Bybit confirm);
  * Chainlink is intentionally NOT in this rotation — it's the resolution
    oracle, consumed directly by the chainlink lane via connectors.chainlink.

All HTTP goes through `_get_json` so tests can inject responses.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_ORDER = ("coinbase", "kraken", "bitstamp", "okx")
FAILS_TO_TRIP = 3
COOLDOWN_SEC = 120.0
TIMEOUT = 6.0

SYMBOLS: dict[str, dict[str, str]] = {
    "coinbase": {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"},
    "kraken": {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"},
    "bitstamp": {"BTC": "btcusd", "ETH": "ethusd", "SOL": "solusd"},
    "okx": {"BTC": "BTC-USDT", "ETH": "ETH-USDT", "SOL": "SOL-USDT"},
}


def _get_json(url: str, params: Optional[dict] = None) -> Any:
    """Single HTTP seam (mocked in tests)."""
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "hermes-cex/2.0"}) as c:
        resp = c.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


@dataclass
class Quote:
    bid: float
    ask: float
    mid: float
    source: str


# --- per-venue mid fetchers -------------------------------------------------

def _mid_coinbase(asset: str) -> Optional[Quote]:
    sym = SYMBOLS["coinbase"].get(asset)
    if not sym:
        return None
    d = _get_json(f"https://api.exchange.coinbase.com/products/{sym}/ticker")
    bid, ask = float(d.get("bid") or 0), float(d.get("ask") or 0)
    last = float(d.get("price") or 0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    return Quote(bid or mid, ask or mid, mid, "coinbase") if mid > 0 else None


def _mid_kraken(asset: str) -> Optional[Quote]:
    sym = SYMBOLS["kraken"].get(asset)
    if not sym:
        return None
    d = _get_json("https://api.kraken.com/0/public/Ticker", {"pair": sym})
    result = d.get("result") or {}
    if not result:
        return None
    row = next(iter(result.values()))
    bid = float((row.get("b") or [0])[0])
    ask = float((row.get("a") or [0])[0])
    last = float((row.get("c") or [0])[0])
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    return Quote(bid or mid, ask or mid, mid, "kraken") if mid > 0 else None


def _mid_bitstamp(asset: str) -> Optional[Quote]:
    sym = SYMBOLS["bitstamp"].get(asset)
    if not sym:
        return None
    d = _get_json(f"https://www.bitstamp.net/api/v2/ticker/{sym}/")
    bid, ask = float(d.get("bid") or 0), float(d.get("ask") or 0)
    last = float(d.get("last") or 0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    return Quote(bid or mid, ask or mid, mid, "bitstamp") if mid > 0 else None


def _mid_okx(asset: str) -> Optional[Quote]:
    sym = SYMBOLS["okx"].get(asset)
    if not sym:
        return None
    d = _get_json("https://www.okx.com/api/v5/market/ticker", {"instId": sym})
    rows = d.get("data") or []
    if not rows:
        return None
    row = rows[0]
    bid, ask = float(row.get("bidPx") or 0), float(row.get("askPx") or 0)
    last = float(row.get("last") or 0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    return Quote(bid or mid, ask or mid, mid, "okx") if mid > 0 else None


# --- per-venue 1m-kline OPEN fetchers ---------------------------------------

def _kline_coinbase(asset: str, ts: int) -> Optional[float]:
    sym = SYMBOLS["coinbase"].get(asset)
    if not sym:
        return None
    start = ts - (ts % 60)
    rows = _get_json(
        f"https://api.exchange.coinbase.com/products/{sym}/candles",
        {"granularity": 60, "start": start, "end": start + 60},
    )
    # rows: [[time, low, high, open, close, volume], ...]
    if isinstance(rows, list) and rows:
        open_px = float(rows[-1][3])
        return open_px if open_px > 0 else None
    return None


def _kline_kraken(asset: str, ts: int) -> Optional[float]:
    sym = SYMBOLS["kraken"].get(asset)
    if not sym:
        return None
    start = ts - (ts % 60)
    d = _get_json(
        "https://api.kraken.com/0/public/OHLC",
        {"pair": sym, "interval": 1, "since": start - 60},
    )
    result = d.get("result") or {}
    rows = next((v for k, v in result.items() if k != "last"), [])
    # rows: [[time, open, high, low, close, vwap, vol, count], ...]
    for row in rows:
        if int(row[0]) == start:
            open_px = float(row[1])
            return open_px if open_px > 0 else None
    return None


def _kline_bitstamp(asset: str, ts: int) -> Optional[float]:
    sym = SYMBOLS["bitstamp"].get(asset)
    if not sym:
        return None
    start = ts - (ts % 60)
    d = _get_json(
        f"https://www.bitstamp.net/api/v2/ohlc/{sym}/",
        {"step": 60, "limit": 2, "start": start},
    )
    rows = ((d.get("data") or {}).get("ohlc")) or []
    for row in rows:
        if int(row.get("timestamp") or 0) == start:
            open_px = float(row.get("open") or 0)
            return open_px if open_px > 0 else None
    return None


def _kline_okx(asset: str, ts: int) -> Optional[float]:
    sym = SYMBOLS["okx"].get(asset)
    if not sym:
        return None
    start_ms = (ts - (ts % 60)) * 1000
    d = _get_json(
        "https://www.okx.com/api/v5/market/history-candles",
        {"instId": sym, "bar": "1m", "after": start_ms + 60_000, "limit": 1},
    )
    rows = d.get("data") or []
    # rows: [[ts_ms, open, high, low, close, ...], ...]
    for row in rows:
        if int(row[0]) == start_ms:
            open_px = float(row[1])
            return open_px if open_px > 0 else None
    return None


# --- registry + circuit breaker ---------------------------------------------

@dataclass
class SourceHealth:
    fails: int = 0
    cooldown_until: float = 0.0

    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def ok(self) -> None:
        self.fails = 0

    def fail(self) -> None:
        self.fails += 1
        if self.fails >= FAILS_TO_TRIP:
            self.cooldown_until = time.time() + COOLDOWN_SEC
            self.fails = 0
            logger.warning("cex source cooling down for %ss", COOLDOWN_SEC)


_MID_FNS: dict[str, Callable[[str], Optional[Quote]]] = {
    "coinbase": _mid_coinbase,
    "kraken": _mid_kraken,
    "bitstamp": _mid_bitstamp,
    "okx": _mid_okx,
}
_KLINE_FNS: dict[str, Callable[[str, int], Optional[float]]] = {
    "coinbase": _kline_coinbase,
    "kraken": _kline_kraken,
    "bitstamp": _kline_bitstamp,
    "okx": _kline_okx,
}

_HEALTH: dict[str, SourceHealth] = {}
_HEALTH_LOCK = threading.Lock()


def source_order() -> list[str]:
    raw = os.environ.get("HERMES_CEX_SOURCES", ",".join(DEFAULT_ORDER))
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    return [n for n in names if n in _MID_FNS] or list(DEFAULT_ORDER)


def _health(name: str) -> SourceHealth:
    with _HEALTH_LOCK:
        return _HEALTH.setdefault(name, SourceHealth())


def get_mid_multi(asset: str, k: int = 2) -> list[Quote]:
    """Up to k independent venue quotes, healthiest-first order."""
    out: list[Quote] = []
    for name in source_order():
        if len(out) >= k:
            break
        h = _health(name)
        if not h.available():
            continue
        try:
            q = _MID_FNS[name](asset.upper())
        except Exception as exc:  # noqa: BLE001
            logger.debug("mid %s/%s failed: %s", name, asset, exc)
            h.fail()
            continue
        if q is None:
            h.fail()
            continue
        h.ok()
        out.append(q)
    return out


def get_mid(asset: str) -> float:
    quotes = get_mid_multi(asset, k=1)
    return quotes[0].mid if quotes else 0.0


def kline_open_at(asset: str, ts: int) -> float:
    """1-minute candle OPEN at ``ts`` from the first venue that has it."""
    for name in source_order():
        h = _health(name)
        if not h.available():
            continue
        try:
            px = _KLINE_FNS[name](asset.upper(), int(ts))
        except Exception as exc:  # noqa: BLE001
            logger.debug("kline %s/%s@%s failed: %s", name, asset, ts, exc)
            h.fail()
            continue
        if px is None or px <= 0:
            # Not a venue failure per se (may just lack the candle) — try next
            continue
        h.ok()
        return float(px)
    return 0.0
