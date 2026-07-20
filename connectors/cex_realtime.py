"""Real-time CEX prices — multi-venue rotation (Coinbase/Kraken/Bitstamp/OKX).

Binance is fully removed: it is geo-blocked (HTTP 451) from the VPS, and the
old Binance-first chain burned seconds of dead retries on every fetch while
rate-limiting the fallback venue. All price access now routes through
``connectors.cex_sources`` (health-aware rotation + circuit breakers).

Public interface is unchanged for consumers:
  get_feed / get_btc_snapshot / get_asset_mid / get_asset_price_history /
  get_asset_snapshot / price_at_timestamp / BtcSnapshot / BtcTick

BtcSnapshot.binance / .bybit are legacy field names now carrying the PRIMARY
and SECONDARY venue ticks respectively (dashboard + agreement checks read
them); the tick's ``source`` says which venue it truly is.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from connectors import cex_sources

# Module-level rolling mids per asset (poll thread + call-sites accumulate).
_ASSET_HISTORY: dict[str, list[tuple[float, float]]] = {}
_ASSET_HISTORY_LOCK = threading.Lock()
_ASSET_HISTORY_MAX_SEC = 600.0

logger = logging.getLogger(__name__)

POLL_SEC = 4.0  # feed thread cadence — dense enough for 30/60/180s momentum
AGREE_BPS = 15.0  # cross-venue agreement threshold


@dataclass
class BtcTick:
    price: float
    bid: float = 0.0
    ask: float = 0.0
    source: str = "none"
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stale: bool = False


@dataclass
class BtcSnapshot:
    """Multi-venue BTC mid with short-horizon momentum.

    ``binance``/``bybit`` are LEGACY names: primary / secondary venue ticks.
    """

    binance: Optional[BtcTick] = None
    bybit: Optional[BtcTick] = None
    mid: float = 0.0
    ret_30s: float = 0.0
    ret_60s: float = 0.0
    ret_3m: float = 0.0
    momentum: float = 0.0  # signed, ~[-1,1]
    sources_agree: bool = True
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class RealtimeBtcFeed:
    """Singleton-ish feed: REST poll thread + rolling price history."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._primary: Optional[BtcTick] = None
        self._secondary: Optional[BtcTick] = None
        self._history: list[tuple[float, float]] = []  # (epoch, price)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        t = threading.Thread(target=self._poll_loop, name="cex-btc-poll", daemon=True)
        t.start()
        self._thread = t
        # Seed immediately so the first turn isn't empty
        self._refresh_rest()
        logger.info(
            "RealtimeBtcFeed started (multi-venue poll: %s)",
            ",".join(cex_sources.source_order()),
        )

    def stop(self) -> None:
        self._stop.set()
        self._started = False

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_rest()
            except Exception as exc:  # noqa: BLE001
                logger.debug("poll refresh failed: %s", exc)
            self._stop.wait(POLL_SEC)

    def _push_history(self, price: float) -> None:
        now = time.time()
        with self._lock:
            self._history.append((now, price))
            cutoff = now - _ASSET_HISTORY_MAX_SEC
            self._history = [(t, p) for t, p in self._history if t >= cutoff]

    def _ret_over(self, seconds: float) -> float:
        now = time.time()
        with self._lock:
            if not self._history:
                return 0.0
            latest = self._history[-1][1]
            target = now - seconds
            older = None
            for t, p in self._history:
                if t <= target:
                    older = p
                else:
                    break
            if older is None or older <= 0:
                older = self._history[0][1]
            if older <= 0:
                return 0.0
            return (latest - older) / older

    def _refresh_rest(self) -> None:
        quotes = cex_sources.get_mid_multi("BTC", k=2)
        now = datetime.now(timezone.utc)
        with self._lock:
            if quotes:
                q = quotes[0]
                self._primary = BtcTick(
                    price=q.mid, bid=q.bid, ask=q.ask, source=q.source, ts=now
                )
            if len(quotes) > 1:
                q2 = quotes[1]
                self._secondary = BtcTick(
                    price=q2.mid, bid=q2.bid, ask=q2.ask, source=q2.source, ts=now
                )
        if quotes:
            self._push_history(quotes[0].mid)
            _push_asset_history("BTC", quotes[0].mid)

    def get_price_history(
        self, max_points: int = 240
    ) -> tuple[list[float], list[float]]:
        """Recent (times, prices) for advanced ensemble — oldest → newest."""
        with self._lock:
            hist = list(self._history)
        if not hist:
            return [], []
        n = max(1, int(max_points))
        hist = hist[-n:]
        return [float(t) for t, _ in hist], [float(p) for _, p in hist]

    def get_top_of_book(self) -> tuple[Optional[float], Optional[float], float, float]:
        """(bid, ask, bid_sz_proxy, ask_sz_proxy) from the primary venue."""
        with self._lock:
            tick = self._primary
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            return None, None, 0.0, 0.0
        return float(tick.bid), float(tick.ask), 1.0, 1.0

    def get_snapshot(self, *, force_rest: bool = False) -> BtcSnapshot:
        if not self._started:
            self.start()
        with self._lock:
            tick = self._primary
        if force_rest or tick is None or (
            (datetime.now(timezone.utc) - tick.ts).total_seconds() > 2 * POLL_SEC + 1
        ):
            self._refresh_rest()

        with self._lock:
            pri = self._primary
            sec = self._secondary

        mid = 0.0
        if pri and pri.price > 0:
            mid = pri.price
            if (datetime.now(timezone.utc) - pri.ts).total_seconds() > 15:
                pri = BtcTick(
                    price=pri.price, bid=pri.bid, ask=pri.ask,
                    source=pri.source, ts=pri.ts, stale=True,
                )
        elif sec and sec.price > 0:
            mid = sec.price

        r30 = self._ret_over(30)
        r60 = self._ret_over(60)
        r3m = self._ret_over(180)
        raw_m = 0.5 * r30 + 0.3 * r60 + 0.2 * r3m
        # ~0.15% move → strong signal on 5m horizon
        momentum = max(-1.0, min(1.0, raw_m / 0.0015))

        agree = True
        if pri and sec and pri.price > 0 and sec.price > 0:
            agree = abs(pri.price - sec.price) / pri.price < AGREE_BPS / 10_000.0

        return BtcSnapshot(
            binance=pri,  # legacy field name: PRIMARY venue tick
            bybit=sec,    # legacy field name: SECONDARY venue tick
            mid=mid,
            ret_30s=r30,
            ret_60s=r60,
            ret_3m=r3m,
            momentum=momentum,
            sources_agree=agree,
            ts=datetime.now(timezone.utc),
        )


_FEED: Optional[RealtimeBtcFeed] = None
_FEED_LOCK = threading.Lock()


def get_feed() -> RealtimeBtcFeed:
    global _FEED
    with _FEED_LOCK:
        if _FEED is None:
            _FEED = RealtimeBtcFeed()
            _FEED.start()
        return _FEED


def get_btc_snapshot(*, force_rest: bool = False) -> BtcSnapshot:
    return get_feed().get_snapshot(force_rest=force_rest)


def _push_asset_history(asset: str, price: float) -> None:
    now = time.time()
    key = asset.upper()
    with _ASSET_HISTORY_LOCK:
        hist = _ASSET_HISTORY.setdefault(key, [])
        hist.append((now, float(price)))
        cutoff = now - _ASSET_HISTORY_MAX_SEC
        _ASSET_HISTORY[key] = [(t, p) for t, p in hist if t >= cutoff]


def get_asset_price_history(
    asset: str, max_points: int = 240
) -> tuple[list[float], list[float]]:
    """(times, prices) for any asset — BTC from the feed, alts from REST cache."""
    asset_u = (asset or "BTC").upper()
    if asset_u == "BTC":
        return get_feed().get_price_history(max_points=max_points)
    with _ASSET_HISTORY_LOCK:
        hist = list(_ASSET_HISTORY.get(asset_u, []))
    if not hist:
        return [], []
    hist = hist[-max(1, int(max_points)) :]
    return [float(t) for t, _ in hist], [float(p) for _, p in hist]


def get_asset_mid(asset: str, *, force_rest: bool = False) -> float:
    """Multi-venue mid for BTC / ETH / SOL (BTC prefers the polling feed)."""
    asset_u = (asset or "BTC").upper()
    if asset_u == "BTC":
        return get_btc_snapshot(force_rest=force_rest).mid
    mid = cex_sources.get_mid(asset_u)
    if mid > 0:
        _push_asset_history(asset_u, mid)
    return mid


def price_at_timestamp(asset: str, ts: int) -> float:
    """CEX price at (or just before) ``ts`` — window-open/close references.

    1-minute candle OPEN covering ``ts`` from the first healthy venue
    (Coinbase → Kraken → Bitstamp → OKX). Returns 0.0 when unavailable so
    callers skip settlement rather than fabricate an outcome.
    """
    return cex_sources.kline_open_at(asset, int(ts))


def _asset_momentum_from_history(asset: str) -> tuple[float, float, float, float]:
    """Compute (ret_30s, ret_60s, ret_3m, momentum) from rolling asset history."""
    times, prices = get_asset_price_history(asset, max_points=240)
    if len(prices) < 2:
        return 0.0, 0.0, 0.0, 0.0
    now = times[-1]
    latest = prices[-1]

    def ret_over(sec: float) -> float:
        older = None
        for t, p in zip(times, prices):
            if t <= now - sec:
                older = p
            else:
                break
        if older is None or older <= 0:
            older = prices[0]
        if older <= 0:
            return 0.0
        return (latest - older) / older

    r30 = ret_over(30)
    r60 = ret_over(60)
    r3m = ret_over(180)
    raw_m = 0.5 * r30 + 0.3 * r60 + 0.2 * r3m
    momentum = max(-1.0, min(1.0, raw_m / 0.0015))
    return r30, r60, r3m, momentum


def get_asset_snapshot(asset: str, *, force_rest: bool = False) -> BtcSnapshot:
    """BtcSnapshot-shaped view for any asset (BTC uses the real feed)."""
    asset_u = (asset or "BTC").upper()
    if asset_u == "BTC":
        return get_btc_snapshot(force_rest=force_rest)
    mid = get_asset_mid(asset_u, force_rest=True)
    r30, r60, r3m, mom = _asset_momentum_from_history(asset_u)
    tick = (
        BtcTick(price=mid, bid=mid, ask=mid, source="multi_venue_rest")
        if mid > 0
        else None
    )
    return BtcSnapshot(
        binance=tick,
        bybit=None,
        mid=mid,
        ret_30s=r30,
        ret_60s=r60,
        ret_3m=r3m,
        momentum=mom,
        sources_agree=True,
        ts=datetime.now(timezone.utc),
    )
