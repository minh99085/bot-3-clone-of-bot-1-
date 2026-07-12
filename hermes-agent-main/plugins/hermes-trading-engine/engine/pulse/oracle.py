"""Oracle feed-type policy + lead-feed feature tracking for the BTC pulse.

Reference model (correct): the trade target is the probability that the **Chainlink Data
Streams reference price** for ``btc/usd`` closes >= it opened over the window. We obtain that
reference price from the **Polymarket RTDS** topic ``crypto_prices_chainlink`` (the exact feed
Polymarket resolves on). Binance/Coinbase are FAST LEAD PREDICTORS only — never settlement.

This module enforces that policy: it rejects classic Chainlink Data Feeds / AggregatorV3 as a
primary settlement feed, and tracks the lead feeds as features (never as truth).
"""

from __future__ import annotations

import time
from typing import Optional

from engine.pulse.fair_value import RollingVol

# The only valid primary settlement oracle for BTC pulse.
CANONICAL_FEED_TYPE = "chainlink_data_streams_refprice"
# Classic on-chain Chainlink price feeds / aggregators are explicitly NOT settlement-valid here
# (too slow + not what Polymarket resolves Up/Down on).
REJECTED_FEED_TYPES = {
    "aggregator_v3", "aggregatorv3", "chainlink_aggregator", "chainlink_data_feed",
    "chainlink_datafeed", "classic_data_feed", "data_feed", "latestrounddata",
}


def validate_oracle_feed_type(feed_type: str) -> str:
    """Return the normalized feed type, or raise ValueError if it's a classic Data Feed /
    AggregatorV3 (rejected) or anything other than the canonical Data Streams ref price."""
    ft = (feed_type or "").strip().lower()
    if ft in REJECTED_FEED_TYPES:
        raise ValueError(
            f"Classic Chainlink Data Feed / AggregatorV3 ({feed_type!r}) is NOT a valid "
            f"primary settlement feed for BTC pulse. Use {CANONICAL_FEED_TYPE!r} "
            "(Chainlink Data Streams reference price via Polymarket RTDS "
            "crypto_prices_chainlink).")
    if ft != CANONICAL_FEED_TYPE:
        raise ValueError(f"Unsupported HERMES_ORACLE_FEED_TYPE {feed_type!r}; "
                         f"expected {CANONICAL_FEED_TYPE!r}.")
    return ft


class LeadFeeds:
    """Tracks fast LEAD feeds (Binance, Coinbase) as FEATURES only — used to predict the
    Chainlink close direction, NEVER as the open/close snapshot or settlement truth."""

    LEAD_ONLY = True

    def __init__(self, fast_feeds: list, *, rtds=None, coinbase_fetcher=None,
                 window_s: float = 900.0):
        self.symbols = list(fast_feeds or [])
        self.rtds = rtds
        self._coinbase = coinbase_fetcher
        self._latest: dict = {}
        self._vol: dict = {s: RollingVol(window_s=window_s, min_samples=8) for s in self.symbols}

    def _read(self, name: str) -> Optional[float]:
        n = name.lower()
        if n.startswith("binance"):
            if self.rtds is not None:
                return self.rtds.latest_price("crypto_prices", "btcusdt")
            return None
        if n.startswith("coinbase"):
            if self._coinbase is None:
                from engine.pulse.coinbase import coinbase_spot_fetcher
                self._coinbase = coinbase_spot_fetcher("BTC-USD")
            return self._coinbase()
        return None

    def poll(self, now: Optional[float] = None) -> None:
        now = float(now if now is not None else time.time())
        for name in self.symbols:
            try:
                px = self._read(name)
            except Exception:  # noqa: BLE001 — a lead feed never breaks a tick
                px = None
            if px and px > 0:
                self._latest[name] = (px, now)
                self._vol[name].observe(px, now)

    def features(self, now: Optional[float] = None) -> dict:
        """Lead features (read-only predictors). Explicitly flagged lead_only so nothing
        downstream can mistake them for settlement truth."""
        out = {"lead_only": True, "feeds": {}}
        for name in self.symbols:
            v = self._latest.get(name)
            out["feeds"][name] = {
                "price": (round(v[0], 2) if v else None),
                "vol_per_sec": self._vol[name].per_sec(now),
                "settlement_eligible": False,
            }
        return out
