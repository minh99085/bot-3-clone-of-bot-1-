"""Pyth Hermes BTC/USD price fetcher (READ-ONLY) — a fast multi-venue aggregate proxy.

Pyth is a low-latency aggregate across many venues (methodologically closer to Chainlink's
benchmark than a single exchange), free via the Hermes REST API, with ~sub-3s freshness and
~20ms request latency. Used as a fast proxy when Chainlink Data Streams credentials are not
configured. Never trades; never raises into the loop.
"""

from __future__ import annotations

from typing import Optional

HERMES = "https://hermes.pyth.network/v2/updates/price/latest"
# Pyth price-feed id for BTC/USD (Crypto.BTC/USD).
BTC_USD_FEED = "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"


def pyth_fetcher(feed_id: str = BTC_USD_FEED, *, timeout_s: float = 4.0):
    """Build a READ-ONLY fetcher ``() -> float | None`` for the Pyth BTC/USD price."""
    box: dict = {}

    def _fetch() -> Optional[float]:
        try:
            import httpx
            c = box.get("c")
            if c is None:
                c = httpx.Client(timeout=timeout_s,
                                 headers={"User-Agent": "hermes-btc-pulse/1.0"})
                box["c"] = c
            r = c.get(HERMES, params={"ids[]": feed_id})
            if r.status_code != 200:
                return None
            parsed = (r.json() or {}).get("parsed") or []
            if not parsed:
                return None
            p = parsed[0].get("price") or {}
            price = int(p["price"]) * (10 ** int(p["expo"]))
            return price if price > 0 else None
        except Exception:  # noqa: BLE001
            return None
    return _fetch
