"""CLOB book feed with optional WebSocket + REST fallback (Roan Part V data pipeline).

PAPER ONLY — read-only market data; measures fetch latency for ops dashboard.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("hte.pulse.clob_feed")

CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class ClobBookFeed:
    """Lightweight book cache; WebSocket when enabled, else REST hydrate callback."""

    def __init__(self, *, websocket_enabled: bool = True):
        self.websocket_enabled = bool(websocket_enabled)
        self._cache: dict[str, dict] = {}
        # live per-token book maintained from the WS stream (book snapshots + price_change deltas):
        # {asset_id: {"bids": {price_str: size}, "asks": {price_str: size}, "ts": epoch, "tick": tick}}
        self._books: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._last_fetch_ms: dict[str, float] = {}
        self._errors = 0
        self._ws_running = False
        self._subscribed: set[str] = set()
        self._msgs = 0

    def record_fetch(self, token_id: str, elapsed_ms: float) -> None:
        with self._lock:
            self._last_fetch_ms[token_id] = round(elapsed_ms, 2)

    def latency_report(self) -> dict:
        with self._lock:
            vals = list(self._last_fetch_ms.values())
        with self._lock:
            live_books = sum(1 for b in self._books.values()
                             if b.get("bids") or b.get("asks"))
            msgs = self._msgs
        base = {
            "samples": len(vals),
            "avg_ms": round(sum(vals) / len(vals), 2) if vals else None,
            "max_ms": round(max(vals), 2) if vals else None,
            "websocket_enabled": self.websocket_enabled,
            "ws_running": self._ws_running,
            "ws_subscribed": len(self._subscribed),
            "ws_msgs": msgs,
            "ws_live_books": live_books,
            "errors": self._errors,
        }
        return base

    def start_ws_background(self, token_ids: list[str]) -> None:
        """Best-effort WS subscriber; fails open to REST."""
        if not self.websocket_enabled or not token_ids:
            return
        new = [t for t in token_ids if t and t not in self._subscribed]
        if not new:
            return
        self._subscribed.update(new)

        def _run():
            from websockets.sync.client import connect
            self._ws_running = True
            backoff = 1.0
            while self._ws_running:
                try:
                    with connect(CLOB_WS, open_timeout=10) as ws:
                        backoff = 1.0
                        subbed = set(self._subscribed)
                        ws.send(json.dumps({"assets_ids": list(subbed), "type": "market"}))
                        last_sub_check = time.time()
                        while self._ws_running:
                            try:
                                raw = ws.recv(timeout=5.0)
                            except TimeoutError:
                                raw = None    # quiet 5s -> keep the connection, do the re-sub check
                            except Exception:
                                break          # real disconnect -> reconnect via the outer loop
                            if raw is not None:
                                try:
                                    msg = json.loads(raw)
                                    for ev in (msg if isinstance(msg, list) else [msg]):
                                        self._ingest(ev)
                                except Exception:
                                    pass       # a bad message never drops the connection
                            # rolling coverage: pick up tokens for newly-opened windows without a full
                            # reconnect by re-sending the subscription when the tracked set has grown.
                            if time.time() - last_sub_check >= 10.0:
                                last_sub_check = time.time()
                                cur = set(self._subscribed)
                                if cur - subbed:
                                    try:
                                        ws.send(json.dumps({"assets_ids": list(cur), "type": "market"}))
                                        subbed = cur
                                    except Exception:
                                        break
                except Exception as exc:
                    logger.debug("clob ws feed reconnect: %s", exc)
                    self._errors += 1
                    if not self._ws_running:
                        break
                    time.sleep(min(30.0, backoff))
                    backoff = min(30.0, backoff * 1.5)
            self._ws_running = False

        threading.Thread(target=_run, daemon=True, name="clob-book-ws").start()

    def _ingest(self, ev: dict) -> None:
        """Maintain the live per-token book from a WS event. ``book`` = full snapshot (replace levels);
        ``price_change`` = deltas (set the level's new absolute size at each price/side; 0 removes it)."""
        if not isinstance(ev, dict):
            return
        aid = ev.get("asset_id") or ev.get("market")
        if not aid:
            return
        aid = str(aid)
        et = ev.get("event_type") or ("book" if ev.get("bids") is not None else None)
        with self._lock:
            self._msgs += 1
            self._cache[aid] = ev
            b = self._books.get(aid)
            if et == "book":
                bids = {str(x["price"]): float(x["size"]) for x in (ev.get("bids") or [])
                        if x.get("price") is not None and float(x.get("size") or 0) > 0}
                asks = {str(x["price"]): float(x["size"]) for x in (ev.get("asks") or [])
                        if x.get("price") is not None and float(x.get("size") or 0) > 0}
                self._books[aid] = {"bids": bids, "asks": asks, "ts": time.time(),
                                    "tick": float(ev.get("tick_size") or 0.01)}
            elif et == "price_change" and b is not None:
                for ch in (ev.get("changes") or []):
                    try:
                        price = str(ch["price"]); size = float(ch.get("size") or 0)
                    except (KeyError, TypeError, ValueError):
                        continue
                    side = str(ch.get("side") or "").upper()
                    book_side = b["bids"] if side in ("BUY", "BID") else b["asks"]
                    if size > 0:
                        book_side[price] = size
                    else:
                        book_side.pop(price, None)
                b["ts"] = time.time()

    def order_book(self, token_id: str, *, max_age_s: float = 30.0):
        """Build an :class:`OrderBook` from the live WS book for ``token_id`` (or None if missing/stale).
        Used as a fast, no-REST TRIGGER for arb detection; a REST fetch re-confirms before booking."""
        with self._lock:
            b = self._books.get(str(token_id))
            if not b:
                return None
            if max_age_s > 0 and (time.time() - float(b.get("ts") or 0)) > max_age_s:
                return None
            bids = sorted(((float(p), s) for p, s in b["bids"].items()), key=lambda x: -x[0])
            asks = sorted(((float(p), s) for p, s in b["asks"].items()), key=lambda x: x[0])
            tick = float(b.get("tick") or 0.01)
        from engine.pulse.markets import OrderBook
        return OrderBook(
            best_bid=(bids[0][0] if bids else None), best_ask=(asks[0][0] if asks else None),
            bid_depth_usd=round(sum(p * s for p, s in bids), 2),
            ask_depth_usd=round(sum(p * s for p, s in asks), 2),
            asks=asks, bids=bids, ts=time.time())