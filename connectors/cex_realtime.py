"""Real-time BTC perpetual prices — Binance (primary WS) + Bybit (confirm).

Designed for 24/7 paper loop:
  - Background WebSocket thread for Binance BTCUSDT perpetual bookTicker
  - REST mark-price fallback if WS drops
  - Optional Bybit linear ticker as secondary confirmation

Consumers call `get_btc_snapshot()` — never block on WS connect.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BINANCE_FUTURES_WS = os.environ.get(
    "BINANCE_FUTURES_WS",
    "wss://fstream.binance.com/ws/btcusdt@bookTicker",
)
BINANCE_FUTURES_REST = os.environ.get(
    "BINANCE_FUTURES_REST",
    "https://fapi.binance.com",
)
BYBIT_REST = os.environ.get("BYBIT_REST", "https://api.bybit.com")
ENABLE_BYBIT = os.environ.get("HERMES_BYBIT_CONFIRM", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)


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
    """Multi-source BTC mid with short-horizon momentum."""

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
    """Singleton-ish feed: WS thread + rolling price history for momentum."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._binance: Optional[BtcTick] = None
        self._bybit: Optional[BtcTick] = None
        self._history: list[tuple[float, float]] = []  # (epoch, price)
        self._ws_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        t = threading.Thread(target=self._ws_loop, name="binance-btc-ws", daemon=True)
        t.start()
        self._ws_thread = t
        # Seed immediately via REST so first turn isn't empty
        self._refresh_rest()
        logger.info("RealtimeBtcFeed started (Binance WS + REST fallback)")

    def stop(self) -> None:
        self._stop.set()
        self._started = False

    def _push_history(self, price: float) -> None:
        now = time.time()
        with self._lock:
            self._history.append((now, price))
            # Keep ~10 minutes
            cutoff = now - 600
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
                # earliest available
                older = self._history[0][1]
            if older <= 0:
                return 0.0
            return (latest - older) / older

    def _ws_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_ws_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Binance WS error: %s — REST fallback 5s", exc)
                self._refresh_rest()
                self._stop.wait(5.0)

    def _run_ws_once(self) -> None:
        try:
            import websocket  # websocket-client
        except ImportError as exc:
            raise RuntimeError("websocket-client not installed") from exc

        def on_message(_ws, message: str) -> None:
            try:
                data = json.loads(message)
                bid = float(data.get("b") or data.get("bidPrice") or 0)
                ask = float(data.get("a") or data.get("askPrice") or 0)
                if bid <= 0 or ask <= 0:
                    return
                mid = (bid + ask) / 2.0
                tick = BtcTick(
                    price=mid,
                    bid=bid,
                    ask=ask,
                    source="binance_ws",
                    ts=datetime.now(timezone.utc),
                )
                with self._lock:
                    self._binance = tick
                self._push_history(mid)
            except Exception as exc:  # noqa: BLE001
                logger.debug("ws parse: %s", exc)

        def on_error(_ws, error) -> None:
            logger.debug("ws on_error: %s", error)

        def on_close(_ws, *_args) -> None:
            logger.info("Binance WS closed")

        ws = websocket.WebSocketApp(
            BINANCE_FUTURES_WS,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        # run_forever blocks until disconnect
        ws.run_forever(ping_interval=20, ping_timeout=10)

    def _refresh_rest(self) -> None:
        # Binance mark price
        try:
            url = f"{BINANCE_FUTURES_REST}/fapi/v1/ticker/bookTicker"
            with httpx.Client(timeout=8.0) as client:
                resp = client.get(url, params={"symbol": "BTCUSDT"})
                resp.raise_for_status()
                data = resp.json()
            bid = float(data.get("bidPrice") or 0)
            ask = float(data.get("askPrice") or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                tick = BtcTick(
                    price=mid, bid=bid, ask=ask, source="binance_rest",
                    ts=datetime.now(timezone.utc),
                )
                with self._lock:
                    # Don't overwrite fresher WS tick
                    if self._binance is None or self._binance.source != "binance_ws":
                        self._binance = tick
                    elif (datetime.now(timezone.utc) - self._binance.ts).total_seconds() > 5:
                        self._binance = tick
                self._push_history(mid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Binance futures REST failed: %s — trying spot", exc)
            try:
                url = "https://api.binance.com/api/v3/ticker/bookTicker"
                with httpx.Client(timeout=8.0) as client:
                    resp = client.get(url, params={"symbol": "BTCUSDT"})
                    resp.raise_for_status()
                    data = resp.json()
                bid = float(data.get("bidPrice") or 0)
                ask = float(data.get("askPrice") or 0)
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2.0
                    tick = BtcTick(
                        price=mid, bid=bid, ask=ask, source="binance_spot_rest",
                        ts=datetime.now(timezone.utc),
                    )
                    with self._lock:
                        self._binance = tick
                    self._push_history(mid)
            except Exception as exc2:  # noqa: BLE001
                logger.warning("Binance spot REST failed: %s", exc2)

        if ENABLE_BYBIT:
            try:
                url = f"{BYBIT_REST}/v5/market/tickers"
                with httpx.Client(timeout=8.0) as client:
                    resp = client.get(url, params={"category": "linear", "symbol": "BTCUSDT"})
                    resp.raise_for_status()
                    data = resp.json()
                rows = (data.get("result") or {}).get("list") or []
                if rows:
                    row = rows[0]
                    last = float(row.get("lastPrice") or 0)
                    bid = float(row.get("bid1Price") or last)
                    ask = float(row.get("ask1Price") or last)
                    if last > 0:
                        with self._lock:
                            self._bybit = BtcTick(
                                price=last,
                                bid=bid,
                                ask=ask,
                                source="bybit_rest",
                                ts=datetime.now(timezone.utc),
                            )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Bybit confirm failed: %s", exc)

    def get_snapshot(self, *, force_rest: bool = False) -> BtcSnapshot:
        if not self._started:
            self.start()
        if force_rest:
            self._refresh_rest()
        else:
            # Soft refresh Bybit + ensure Binance not stale
            with self._lock:
                bn = self._binance
            if bn is None or (datetime.now(timezone.utc) - bn.ts).total_seconds() > 8:
                self._refresh_rest()
            elif ENABLE_BYBIT and (
                self._bybit is None
                or (datetime.now(timezone.utc) - self._bybit.ts).total_seconds() > 30
            ):
                self._refresh_rest()

        with self._lock:
            bn = self._binance
            by = self._bybit

        mid = 0.0
        if bn and bn.price > 0:
            mid = bn.price
            if bn.source != "binance_ws" and (datetime.now(timezone.utc) - bn.ts).total_seconds() > 15:
                bn = BtcTick(**{**bn.__dict__, "stale": True}) if False else bn
                # mark stale via copy
                bn = BtcTick(
                    price=bn.price, bid=bn.bid, ask=bn.ask, source=bn.source, ts=bn.ts, stale=True
                )
        elif by and by.price > 0:
            mid = by.price

        r30 = self._ret_over(30)
        r60 = self._ret_over(60)
        r3m = self._ret_over(180)
        # Momentum score: blend short returns, clip
        raw_m = 0.5 * r30 + 0.3 * r60 + 0.2 * r3m
        # ~0.15% move → strong signal on 5m horizon
        momentum = max(-1.0, min(1.0, raw_m / 0.0015))

        agree = True
        if bn and by and bn.price > 0 and by.price > 0:
            agree = abs(bn.price - by.price) / bn.price < 0.0015  # 15 bps

        return BtcSnapshot(
            binance=bn,
            bybit=by,
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
