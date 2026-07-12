"""Low-latency BTC price feed + per-window OPEN-price snapshots (READ-ONLY).

Resolution uses the Chainlink BTC/USD Data Stream. That feed is credentialed, so we use
a free low-latency proxy (Coinbase spot) and measure BOTH the window-open and the live
price on the SAME feed — the absolute Coinbase-vs-Chainlink basis then cancels in the
``close - open`` comparison; only the small intra-window basis *drift* remains (handled by
the decision buffer). Never trades; only reads a public price.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

OPEN_SNAPSHOT_SCHEMA = "pulse_open_snapshots/1.0"

from engine.pulse.fair_value import RollingVol

logger = logging.getLogger("hte.pulse.price")


def build_price_source(source: str = "auto"):
    """Pick the price fetcher + a label. ``auto`` uses Chainlink Data Streams (the exact
    resolution feed) when credentials are configured, else the Coinbase proxy. Explicit:
    ``chainlink`` | ``pyth`` | ``coinbase``. Falls back safely when a source is unavailable."""
    src = (source or "auto").strip().lower()
    if src in ("chainlink", "auto"):
        try:
            from engine.pulse.chainlink_streams import available, chainlink_streams_fetcher
            if available():
                return chainlink_streams_fetcher(), "chainlink_data_streams"
        except Exception:  # noqa: BLE001
            pass
        if src == "chainlink":
            logger.warning("PULSE_PRICE_SOURCE=chainlink but no Data Streams creds; "
                           "falling back to coinbase")
    if src == "pyth":
        from engine.pulse.pyth import pyth_fetcher
        return pyth_fetcher(), "pyth"
    from engine.pulse.coinbase import coinbase_spot_fetcher
    return coinbase_spot_fetcher("BTC-USD"), "coinbase"


@dataclass
class OpenSnapshot:
    """The recorded window-open reference price + how late we captured it + which oracle."""
    open_ts: float
    price: float
    snap_ts: float
    source: str = "rtds_chainlink"

    @property
    def lag_s(self) -> float:
        return max(0.0, self.snap_ts - self.open_ts)


class PulsePriceFeed:
    """Polls a BTC spot proxy, feeds a rolling-vol estimator, and snapshots each window's
    open price as soon as the window begins."""

    def __init__(self, *, fetcher=None, vol: Optional[RollingVol] = None,
                 max_open_lag_s: float = 20.0, max_open_lag_15m_s: float = 240.0,
                 source_name: str = "coinbase",
                 sampler_interval_s: float = 0.0,
                 history_seconds: float = 3900.0):
        if fetcher is None:
            from engine.pulse.coinbase import coinbase_spot_fetcher
            fetcher = coinbase_spot_fetcher("BTC-USD")
        self._fetch = fetcher
        self.source_name = source_name
        self.vol = vol or RollingVol()
        self.max_open_lag_s = float(max_open_lag_s)
        self.max_open_lag_15m_s = float(max_open_lag_15m_s)
        self.sampler_interval_s = float(sampler_interval_s)
        self.history_seconds = max(60.0, float(history_seconds))
        self._last_price: Optional[float] = None
        self._last_ts: float = 0.0        # wall-clock of the last SUCCESSFUL fetch (freshness clock)
        self.last_fetch_ok: bool = False  # did the most recent poll get a live value?
        self._opens: dict = {}            # window_key -> OpenSnapshot
        # Timestamped source observations let a window discovered after its boundary recover the
        # real boundary tick.  We never substitute a later "current" price for the opening price.
        self._history: deque = deque(maxlen=20000)
        self._history_lock = threading.Lock()
        self.polls = 0
        self.errors = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start_sampler(self) -> None:
        """Poll the feed on a background daemon at ``sampler_interval_s`` so the price stays
        fresh (and vol fine-grained) BETWEEN the slower trade ticks. No-op if interval<=0."""
        if self.sampler_interval_s <= 0:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop():
            while not self._stop.is_set():
                try:
                    self.poll()
                except Exception:  # noqa: BLE001
                    pass
                self._stop.wait(self.sampler_interval_s)
        self._thread = threading.Thread(target=_loop, name="pulse-price-sampler", daemon=True)
        self._thread.start()

    def stop_sampler(self) -> None:
        self._stop.set()

    def poll(self, now: Optional[float] = None) -> Optional[float]:
        now = float(now if now is not None else time.time())
        try:
            px = self._fetch()
        except Exception:  # noqa: BLE001 — a price read never raises into the loop
            px = None
        if px is not None and px > 0:
            self._last_price = float(px)
            self._last_ts = now           # only advances on a LIVE fetch -> true freshness clock
            self.last_fetch_ok = True
            self.vol.observe(px, now)
            with self._history_lock:
                self._history.append((now, float(px)))
                cutoff = now - self.history_seconds
                while self._history and self._history[0][0] < cutoff:
                    self._history.popleft()
            self.polls += 1
        else:
            self.last_fetch_ok = False    # stale read: keep _last_price but DON'T advance _last_ts
            self.errors += 1
        return self._last_price

    def current(self) -> Optional[float]:
        return self._last_price

    def age_s(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds since the last SUCCESSFUL price fetch (None if never)."""
        if self._last_ts <= 0:
            return None
        return max(0.0, float(now if now is not None else time.time()) - self._last_ts)

    def is_fresh(self, max_age_s: float, now: Optional[float] = None) -> bool:
        """True if we have a price AND it was fetched within ``max_age_s`` (<=0 disables the gate)."""
        if self._last_price is None:
            return False
        if max_age_s is None or max_age_s <= 0:
            return True
        age = self.age_s(now)
        return age is not None and age <= float(max_age_s)

    def sigma_per_sec(self, now: Optional[float] = None) -> Optional[float]:
        return self.vol.per_sec(now)

    def effective_max_open_lag(self, window_seconds: int = 300) -> float:
        """Scale open-lag tolerance: 15m windows tolerate later first capture."""
        ws = int(window_seconds or 300)
        if ws >= 900:
            return float(self.max_open_lag_15m_s)
        return float(self.max_open_lag_s) * (float(ws) / 300.0)

    def snapshot_open(self, key: str, open_ts: float, now: Optional[float] = None,
                      window_seconds: int = 300) -> Optional[OpenSnapshot]:
        """Record the observation nearest the exact window boundary.

        A restart after the boundary must fail closed when the boundary is absent from history;
        using the current price as a synthetic open changes the contract being predicted.
        """
        now = float(now if now is not None else time.time())
        if key in self._opens:
            return self._opens[key]
        if now < open_ts:
            return None
        if self._last_price is None:
            return None
        tolerance = self.effective_max_open_lag(window_seconds)
        with self._history_lock:
            history = list(self._history)
        candidates = [(abs(ts - open_ts), ts, price) for ts, price in history
                      if abs(ts - open_ts) <= tolerance]
        if not candidates:
            return None
        _dist, observed_ts, observed_price = min(candidates, key=lambda row: row[0])
        snap = OpenSnapshot(open_ts=open_ts, price=observed_price, snap_ts=observed_ts,
                            source=self.source_name)
        self._opens[key] = snap
        if snap.lag_s > self.max_open_lag_s:
            logger.debug("open snapshot for %s captured late (lag %.1fs)", key, snap.lag_s)
        return snap

    def open_snapshot(self, key: str) -> Optional[OpenSnapshot]:
        return self._opens.get(key)

    def prune_opens(self, keep_keys: set) -> None:
        """Drop open snapshots for windows no longer tracked (bound memory)."""
        for k in list(self._opens):
            if k not in keep_keys:
                self._opens.pop(k, None)

    def to_open_state(self) -> list[dict]:
        rows = []
        for key, snap in self._opens.items():
            rows.append({
                "key": str(key),
                "open_ts": float(snap.open_ts),
                "price": float(snap.price),
                "snap_ts": float(snap.snap_ts),
                "source": str(snap.source or self.source_name),
            })
        return rows

    def load_open_state(self, rows: list) -> int:
        """Restore persisted open snapshots (survives container restart)."""
        loaded = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            key = row.get("key")
            if not key or key in self._opens:
                continue
            try:
                snap = OpenSnapshot(
                    open_ts=float(row["open_ts"]),
                    price=float(row["price"]),
                    snap_ts=float(row["snap_ts"]),
                    source=str(row.get("source") or self.source_name),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._opens[str(key)] = snap
            loaded += 1
        return loaded

    def status(self) -> dict:
        return {"source": self.source_name, "last_price": self._last_price,
                "last_ts": self._last_ts, "age_s": self.age_s(), "last_fetch_ok": self.last_fetch_ok,
                "polls": self.polls, "errors": self.errors,
                "sampler_interval_s": self.sampler_interval_s,
                "sampler_running": bool(self._thread is not None and self._thread.is_alive()),
                "vol_samples": self.vol.samples,
                "sigma_per_sec": self.sigma_per_sec(), "tracked_opens": len(self._opens),
                "max_open_lag_s": self.max_open_lag_s,
                "max_open_lag_15m_s": self.max_open_lag_15m_s}
