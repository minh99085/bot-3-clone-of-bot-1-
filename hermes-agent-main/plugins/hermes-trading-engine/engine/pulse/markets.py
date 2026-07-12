"""Ingestion of Polymarket BTC rolling up/down windows (READ-ONLY).

Supports the ``btc-up-or-down-5m`` and ``btc-up-or-down-15m`` series (and extensible via
``MultiSeriesMarketFeed``). Each event is a single binary market ("Up"/"Down") over a fixed
window. The OPEN is encoded in the slug (``btc-updown-5m-<open_unix_ts>``) and CLOSE is the
market ``endDate``. Resolution (per the market description) is::

    Up  iff  Chainlink_BTC_close >= Chainlink_BTC_open    (ties -> Up)

This module fetches current/upcoming windows + Up/Down CLOB token ids and live order books.
It never trades; it only reads public Gamma + CLOB endpoints.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("hte.pulse.markets")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SERIES_SLUG_5M = "btc-up-or-down-5m"
SERIES_SLUG_15M = "btc-up-or-down-15m"
SERIES_SLUG_ETH_5M = "eth-up-or-down-5m"
SERIES_SLUG_ETH_15M = "eth-up-or-down-15m"
SERIES_SLUG_SOL_5M = "sol-up-or-down-5m"
SERIES_SLUG_SOL_15M = "sol-up-or-down-15m"
SERIES_SLUG_XRP_5M = "xrp-up-or-down-5m"
SERIES_SLUG_XRP_15M = "xrp-up-or-down-15m"
SERIES_SLUG_DOGE_5M = "doge-up-or-down-5m"
SERIES_SLUG_DOGE_15M = "doge-up-or-down-15m"
SERIES_SLUG_BNB_5M = "bnb-up-or-down-5m"
SERIES_SLUG_BNB_15M = "bnb-up-or-down-15m"
WINDOW_SECONDS = 300


def market_fees_enabled(value) -> bool:
    """Normalize Gamma's boolean whether decoded from JSON or a cached string."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)
WINDOW_SECONDS_15M = 900

_D5 = {"window_seconds": WINDOW_SECONDS, "lookahead_s": 330.0}
_D15 = {"window_seconds": WINDOW_SECONDS_15M, "lookahead_s": 990.0}
SERIES_DEFAULTS = {
    SERIES_SLUG_5M: {**_D5, "label": "5m"},
    SERIES_SLUG_15M: {**_D15, "label": "15m"},
    SERIES_SLUG_ETH_5M: {**_D5, "label": "eth_5m"},
    SERIES_SLUG_ETH_15M: {**_D15, "label": "eth_15m"},
    SERIES_SLUG_SOL_5M: {**_D5, "label": "sol_5m"},
    SERIES_SLUG_SOL_15M: {**_D15, "label": "sol_15m"},
    SERIES_SLUG_XRP_5M: {**_D5, "label": "xrp_5m"},
    SERIES_SLUG_XRP_15M: {**_D15, "label": "xrp_15m"},
    SERIES_SLUG_DOGE_5M: {**_D5, "label": "doge_5m"},
    SERIES_SLUG_DOGE_15M: {**_D15, "label": "doge_15m"},
    SERIES_SLUG_BNB_5M: {**_D5, "label": "bnb_5m"},
    SERIES_SLUG_BNB_15M: {**_D15, "label": "bnb_15m"},
}

# All crypto up/down assets also list HOURLY and DAILY windows (date-based slugs, e.g.
# "bitcoin-up-or-down-on-july-4-2026"), so their event slug carries no updown-Nm-<ts> pattern.
# We MUST supply window_seconds here so parse_window can derive open_ts = close_ts - duration.
# Risk-free arb applies to any window length; longer windows just cross less often and lock capital
# longer (daily up to ~24h), so keep them on the shared exposure cap.
# All crypto up/down assets (incl. HYPE) across ALL time slots: 5m, 15m, 4h, hourly, daily.
# 4h/hourly/daily slugs carry no updown-Nm-<ts> minute pattern, so window_seconds MUST be supplied
# here (parse_window then derives open_ts). SOL's hourly is listed under the "solana" prefix.
for _asset in ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype"):
    SERIES_DEFAULTS.setdefault(
        "%s-up-or-down-5m" % _asset, {**_D5, "label": "%s_5m" % _asset})
    SERIES_DEFAULTS.setdefault(
        "%s-up-or-down-15m" % _asset, {**_D15, "label": "%s_15m" % _asset})
    SERIES_DEFAULTS.setdefault(
        "%s-up-or-down-4h" % _asset,
        {"window_seconds": 14400, "lookahead_s": 600.0, "label": "%s_4h" % _asset})
    SERIES_DEFAULTS.setdefault(
        "%s-up-or-down-hourly" % _asset,
        {"window_seconds": 3600, "lookahead_s": 300.0, "label": "%s_1h" % _asset})
    SERIES_DEFAULTS.setdefault(
        "%s-up-or-down-daily" % _asset,
        {"window_seconds": 86400, "lookahead_s": 900.0, "label": "%s_1d" % _asset})
SERIES_DEFAULTS.setdefault(
    "solana-up-or-down-hourly", {"window_seconds": 3600, "lookahead_s": 300.0, "label": "sol_1h"})
SERIES_DEFAULTS.setdefault(
    "solana-up-or-down-daily", {"window_seconds": 86400, "lookahead_s": 900.0, "label": "sol_1d"})

_SLUG_TS_RE = re.compile(r"-(\d{9,11})$")
_SLUG_DUR_RE = re.compile(r"updown-(\d+)m-", re.I)


def _iso_to_unix(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


@dataclass
class OrderBook:
    """Order book snapshot for one CLOB token (read-only).

    ``asks`` / ``bids`` are full ladders [(price, size_shares), ...] sorted from best to
    worst (asks ascending by price, bids descending) so the execution gate can walk depth and
    compute a realistic VWAP/slippage fill — never just the midpoint or top of book."""
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_depth_usd: float = 0.0
    ask_depth_usd: float = 0.0
    ts: float = 0.0
    asks: list = field(default_factory=list)     # [(price, size_shares)] ascending
    bids: list = field(default_factory=list)     # [(price, size_shares)] descending

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return round((self.best_bid + self.best_ask) / 2.0, 6)
        return self.best_bid if self.best_bid is not None else self.best_ask

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return round(self.best_ask - self.best_bid, 6)
        return None


@dataclass
class PulseWindow:
    """One BTC up/down Polymarket window (5m or 15m series)."""
    event_id: str
    market_id: str
    slug: str
    title: str
    open_ts: float
    close_ts: float
    up_token_id: str
    down_token_id: str
    tick_size: float = 0.01
    series_slug: str = SERIES_SLUG_5M
    window_seconds: int = WINDOW_SECONDS
    series_label: str = "5m"
    up_book: Optional[OrderBook] = None
    down_book: Optional[OrderBook] = None
    # Directional 1h dated / above-strike markets (see directional_hourly_feed.py).
    market_kind: str = "updown"       # "updown" | "above" (Yes/No mapped to up/down)
    strike_price: Optional[float] = None
    directional_lane: bool = False    # True => eligible for directional lane only
    tv_symbol: Optional[str] = None   # lane-specific TV storage key (e.g. BTCUSDT for above lane)
    fees_enabled: bool = False
    taker_fee_rate: float = 0.0       # decimal rate used by fee=C*rate*p*(1-p)

    def seconds_to_close(self, now: Optional[float] = None) -> float:
        return self.close_ts - float(now if now is not None else time.time())

    def seconds_since_open(self, now: Optional[float] = None) -> float:
        return float(now if now is not None else time.time()) - self.open_ts

    def is_open(self, now: Optional[float] = None) -> bool:
        n = float(now if now is not None else time.time())
        return self.open_ts <= n < self.close_ts

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "market_id": self.market_id, "slug": self.slug,
                "title": self.title, "open_ts": self.open_ts, "close_ts": self.close_ts,
                "up_token_id": self.up_token_id, "down_token_id": self.down_token_id,
                "tick_size": self.tick_size, "series_slug": self.series_slug,
                "window_seconds": self.window_seconds, "series_label": self.series_label,
                "up_mid": self.up_book.mid if self.up_book else None,
                "up_spread": self.up_book.spread if self.up_book else None}


class PulseMarketFeed:
    """Read-only client for one Polymarket BTC up/down series + CLOB books."""

    def __init__(self, *, timeout_s: float = 8.0, series_slug: str = SERIES_SLUG_5M,
                 window_seconds: Optional[int] = None, lookahead_s: Optional[float] = None,
                 http_get=None, on_book_fetch=None):
        self.timeout_s = float(timeout_s)
        self.series_slug = series_slug
        self.on_book_fetch = on_book_fetch
        defaults = SERIES_DEFAULTS.get(series_slug, {})
        self.window_seconds = int(window_seconds or defaults.get("window_seconds") or WINDOW_SECONDS)
        self.lookahead_s = float(lookahead_s if lookahead_s is not None
                                 else defaults.get("lookahead_s") or (self.window_seconds + 30))
        self.series_label = str(defaults.get("label") or ("15m" if "15m" in series_slug else "5m"))
        self._get = http_get          # injectable for tests: (url, params) -> (status, json)
        self._client = None

    @staticmethod
    def _window_seconds_from_slug(slug: str, series_slug: str = SERIES_SLUG_5M) -> int:
        m = _SLUG_DUR_RE.search(str(slug or ""))
        if m:
            return int(m.group(1)) * 60
        if "15m" in str(series_slug):
            return WINDOW_SECONDS_15M
        return WINDOW_SECONDS

    def _http(self, url: str, params: dict) -> "tuple[int, object]":
        if self._get is not None:
            return self._get(url, params)
        if self._client is None:
            import httpx
            self._client = httpx.Client(timeout=self.timeout_s,
                                        headers={"User-Agent": "hermes-btc-pulse/1.0"})
        try:
            r = self._client.get(url, params=params)
            return r.status_code, (r.json() if r.status_code == 200 else None)
        except Exception as exc:  # noqa: BLE001 — a read never raises into the loop
            logger.debug("pulse http error %s", exc)
            return 0, None

    @staticmethod
    def parse_window(event: dict, *, series_slug: str = SERIES_SLUG_5M,
                     window_seconds: Optional[int] = None) -> Optional[PulseWindow]:
        """Build a :class:`PulseWindow` from a Gamma event dict (or None if malformed)."""
        try:
            markets = event.get("markets") or []
            if not markets:
                return None
            m = markets[0]
            toks = m.get("clobTokenIds")
            if isinstance(toks, str):
                toks = json.loads(toks or "[]")
            outs = m.get("outcomes")
            if isinstance(outs, str):
                outs = json.loads(outs or "[]")
            if not toks or len(toks) < 2 or len(outs) < 2:
                return None
            # map outcome name -> token (robust to ordering)
            up_tok = down_tok = None
            for name, tok in zip(outs, toks):
                if str(name).strip().lower() == "up":
                    up_tok = str(tok)
                elif str(name).strip().lower() == "down":
                    down_tok = str(tok)
            if up_tok is None or down_tok is None:
                up_tok, down_tok = str(toks[0]), str(toks[1])
            close_ts = _iso_to_unix(m.get("endDate") or event.get("endDate"))
            slug = str(event.get("slug") or "")
            dur_s = int(window_seconds or PulseMarketFeed._window_seconds_from_slug(slug, series_slug))
            defaults = SERIES_DEFAULTS.get(series_slug, {})
            label = str(defaults.get("label") or ("15m" if dur_s >= 900 else "5m"))
            open_ts = None
            mt = _SLUG_TS_RE.search(slug)
            if mt:
                open_ts = float(mt.group(1))
            if close_ts is None and open_ts is not None:
                close_ts = open_ts + dur_s
            if open_ts is None and close_ts is not None:
                open_ts = close_ts - dur_s
            if open_ts is None or close_ts is None:
                return None
            tick = float(m.get("orderPriceMinTickSize") or 0.01)
            return PulseWindow(
                event_id=str(event.get("id") or ""), market_id=str(m.get("id") or ""),
                slug=slug, title=str(event.get("title") or m.get("question") or ""),
                open_ts=float(open_ts), close_ts=float(close_ts),
                up_token_id=up_tok, down_token_id=down_tok, tick_size=tick,
                series_slug=series_slug, window_seconds=dur_s, series_label=label,
                fees_enabled=market_fees_enabled(m.get("feesEnabled")),
                taker_fee_rate=(0.07 if market_fees_enabled(m.get("feesEnabled")) else 0.0))
        except Exception as exc:  # noqa: BLE001
            logger.debug("parse_window failed: %s", exc)
            return None

    def fetch_windows(self, *, limit: int = 60) -> list:
        """Current + upcoming windows for the series, ascending by close time."""
        status, data = self._http(f"{GAMMA}/events",
                                   {"series_slug": self.series_slug, "closed": "false",
                                    "order": "endDate", "ascending": "true",
                                    "limit": int(limit)})
        if status != 200 or not isinstance(data, list):
            return []
        out = []
        for ev in data:
            w = self.parse_window(ev, series_slug=self.series_slug,
                                  window_seconds=self.window_seconds)
            if w is not None:
                out.append(w)
        out.sort(key=lambda w: w.close_ts)
        return out

    def active_windows(self, *, now: Optional[float] = None, lookahead_s: Optional[float] = None,
                       limit: int = 60) -> list:
        """Windows that are open now or open within ``lookahead_s`` (so we can snapshot the
        open price the moment they begin)."""
        lookahead_s = self.lookahead_s if lookahead_s is None else float(lookahead_s)
        n = float(now if now is not None else time.time())
        out = []
        for w in self.fetch_windows(limit=limit):
            if w.close_ts <= n:
                continue                       # already closed
            if w.open_ts <= n + lookahead_s:   # open now or about to open
                out.append(w)
        return out

    def fetch_book(self, token_id: str) -> Optional[OrderBook]:
        """Top-of-book + shallow depth for one token (read-only)."""
        t0 = time.perf_counter()
        status, data = self._http(f"{CLOB}/book", {"token_id": token_id})
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        cb = getattr(self, "on_book_fetch", None)
        if cb is not None:
            try:
                cb(token_id, elapsed_ms)
            except Exception:
                pass
        if status != 200 or not isinstance(data, dict):
            return None
        bids = data.get("bids") or []
        asks = data.get("asks") or []

        def _lvls(side):
            out = []
            for x in side:
                try:
                    out.append((float(x["price"]), float(x["size"])))
                except (KeyError, TypeError, ValueError):
                    continue
            return out
        bids = _lvls(bids)
        asks = _lvls(asks)
        # CLOB returns bids ascending and asks ascending; best bid = highest, best ask = lowest.
        # Store full ladders best->worst (asks ascending, bids descending) for VWAP/depth walks.
        asks_sorted = sorted(asks, key=lambda x: x[0])
        bids_sorted = sorted(bids, key=lambda x: x[0], reverse=True)
        best_bid = bids_sorted[0][0] if bids_sorted else None
        best_ask = asks_sorted[0][0] if asks_sorted else None
        return OrderBook(
            best_bid=best_bid, best_ask=best_ask,
            bid_depth_usd=round(sum(p * s for p, s in bids), 2),
            ask_depth_usd=round(sum(p * s for p, s in asks), 2),
            ts=time.time(), asks=asks_sorted, bids=bids_sorted)

    def hydrate_books(self, window: PulseWindow) -> PulseWindow:
        """Attach live Up/Down books to a window (read-only)."""
        window.up_book = self.fetch_book(window.up_token_id)
        window.down_book = self.fetch_book(window.down_token_id)
        return window

    def fetch_resolution(self, market_id: str) -> Optional[bool]:
        """Authoritative Polymarket resolution for a CLOSED market: returns True if it
        resolved ``Up``, False if ``Down``, or None if not yet resolved. Read-only."""
        status, m = self._http(f"{GAMMA}/markets/{market_id}", {})
        if status != 200 or not isinstance(m, dict):
            return None
        # only trust a genuinely resolved market
        if not (m.get("closed") or m.get("umaResolutionStatus") == "resolved"):
            # outcomePrices may still pin to 0/1 once resolved even if 'closed' lags
            pass
        outs = m.get("outcomes")
        prices = m.get("outcomePrices")
        if isinstance(outs, str):
            outs = json.loads(outs or "[]")
        if isinstance(prices, str):
            prices = json.loads(prices or "[]")
        if not outs or not prices or len(outs) != len(prices):
            return None
        try:
            mapping = {str(o).strip().lower(): float(p) for o, p in zip(outs, prices)}
        except (TypeError, ValueError):
            return None
        for up_name, dn_name in (("up", "down"), ("yes", "no")):
            up = mapping.get(up_name)
            down = mapping.get(dn_name)
            if up is None or down is None:
                continue
            if up >= 0.99 and down <= 0.01:
                return True
            if down >= 0.99 and up <= 0.01:
                return False
        return None


class MultiSeriesMarketFeed:
    """Merge active windows from multiple Polymarket BTC up/down series (e.g. 5m + 15m)."""

    def __init__(self, series_slugs: "tuple | list", *, timeout_s: float = 8.0, http_get=None):
        slugs = tuple(s for s in (series_slugs or ()) if str(s).strip())
        if not slugs:
            slugs = (SERIES_SLUG_5M,)
        self.series_slugs = slugs
        self._feeds: dict = {}
        for slug in slugs:
            self._feeds[slug] = PulseMarketFeed(
                timeout_s=timeout_s, series_slug=slug, http_get=http_get)

    def _feed_for(self, window: PulseWindow) -> PulseMarketFeed:
        return self._feeds.get(window.series_slug) or next(iter(self._feeds.values()))

    def active_windows(self, *, now: Optional[float] = None, limit: int = 60) -> list:
        out = []
        for feed in self._feeds.values():
            out.extend(feed.active_windows(now=now, limit=limit))
        out.sort(key=lambda w: w.close_ts)
        return out

    def hydrate_books(self, window: PulseWindow) -> PulseWindow:
        return self._feed_for(window).hydrate_books(window)

    def fetch_resolution(self, market_id: str) -> Optional[bool]:
        for feed in self._feeds.values():
            res = feed.fetch_resolution(market_id)
            if res is not None:
                return res
        return None

    def report(self) -> dict:
        return {"multi_series": True, "series_slugs": list(self.series_slugs),
                "feeds": {slug: {"window_seconds": f.window_seconds, "lookahead_s": f.lookahead_s,
                                 "series_label": f.series_label}
                          for slug, f in self._feeds.items()}}
