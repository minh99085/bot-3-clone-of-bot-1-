"""Read-only extra CEX spot-price fetchers (Kraken, Bitstamp) for the BTC Pulse edge basket.

Self-contained, short-timeout, fail-open (never raise into the loop, never trade). These are
OPTIONAL basket members — used only as observe-only momentum features. Binance (via RTDS) and
Coinbase (via REST) remain the primary feeds; these add breadth + cross-exchange agreement.
"""

from __future__ import annotations

from typing import Optional

_KRAKEN = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
_BITSTAMP = "https://www.bitstamp.net/api/v2/ticker/btcusd/"


def _client(box: dict, timeout_s: float):
    c = box.get("c")
    if c is None:
        import httpx
        c = httpx.Client(timeout=timeout_s, headers={"User-Agent": "hermes-btc-pulse/1.0"})
        box["c"] = c
    return c


def kraken_spot_fetcher(*, timeout_s: float = 1.5):
    """READ-ONLY Kraken XBT/USD spot fetcher ``() -> float|None``. Short timeout, fail-open."""
    box: dict = {}

    def _fetch() -> Optional[float]:
        try:
            r = _client(box, timeout_s).get(_KRAKEN)
            if r.status_code != 200:
                return None
            result = (r.json() or {}).get("result") or {}
            for _pair, v in result.items():            # e.g. {"XXBTZUSD": {"c": ["63000.0", ...]}}
                c = (v or {}).get("c")
                if c:
                    p = float(c[0])
                    return p if p > 0 else None
            return None
        except Exception:  # noqa: BLE001
            return None
    return _fetch


def bitstamp_spot_fetcher(*, timeout_s: float = 1.5):
    """READ-ONLY Bitstamp BTC/USD spot fetcher ``() -> float|None``. Short timeout, fail-open."""
    box: dict = {}

    def _fetch() -> Optional[float]:
        try:
            r = _client(box, timeout_s).get(_BITSTAMP)
            if r.status_code != 200:
                return None
            last = (r.json() or {}).get("last")
            p = float(last)
            return p if p > 0 else None
        except Exception:  # noqa: BLE001
            return None
    return _fetch
