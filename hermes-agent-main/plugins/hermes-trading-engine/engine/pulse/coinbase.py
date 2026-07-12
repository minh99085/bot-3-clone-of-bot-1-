"""Read-only Coinbase spot-price fetcher for the BTC pulse engine.

Self-contained (no other engine deps) so the pulse package stands alone. Never trades,
never raises into the loop.
"""

from __future__ import annotations

from typing import Optional

_COINBASE_SPOT = "https://api.coinbase.com/v2/prices/{sym}/spot"


def coinbase_spot_fetcher(symbol: str = "BTC-USD", *, timeout_s: float = 4.0):
    """Build a READ-ONLY Coinbase spot-price fetcher for ``symbol`` (e.g. 'BTC-USD').
    Returns a callable ``() -> float | None``; never raises, never trades."""
    url = _COINBASE_SPOT.format(sym=symbol)
    box: dict = {}

    def _client():
        c = box.get("c")
        if c is None:
            import httpx
            c = httpx.Client(timeout=timeout_s,
                             headers={"User-Agent": "hermes-btc-pulse/1.0"})
            box["c"] = c
        return c

    def _fetch() -> Optional[float]:
        try:
            r = _client().get(url)
            if r.status_code != 200:
                return None
            amt = (((r.json() or {}).get("data") or {}).get("amount"))
            p = float(amt)
            return p if p > 0 else None
        except Exception:  # noqa: BLE001 — a price fetch never raises into the loop
            return None
    return _fetch
