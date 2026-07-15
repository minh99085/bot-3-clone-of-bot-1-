"""Polymarket connector — Gamma + Data API + official py-clob-client-v2.

Separation of concerns:
  - Gamma HTTP: market discovery / metadata (no wallet)
  - CLOB SDK: orderbook depth, mid, spread, paper fill simulation inputs
  - Live order placement only when HERMES_LIVE=1 + wallet creds

Paper mode never posts orders; it reads the book and simulates fills.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from hermes.models import MarketCandidate, Regime

logger = logging.getLogger(__name__)

GAMMA_HOST = os.environ.get("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB_HOST = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
DATA_HOST = os.environ.get("POLYMARKET_DATA_HOST", "https://data-api.polymarket.com")
CHAIN_ID = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBookSnapshot:
    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    mid: Optional[float] = None
    spread_bps: float = 0.0
    source: str = "clob"

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None


def infer_timeframe(slug: str, question: str = "") -> str:
    blob = f"{slug} {question}".lower()
    # Check longer windows first so "15m" is not matched as "5m"
    if re.search(r"\b15\s*m\b|15min|15-min|15m-", blob):
        return "15m"
    if re.search(r"(?<!\d)5\s*m\b|(?<!\d)5min|5-min|(?<!\d)5m-", blob):
        return "5m"
    if re.search(r"\b1\s*h\b|1h-|hourly", blob):
        return "1h"
    if "up or down" in blob or "updown" in blob.replace(" ", "") or "up-down" in blob:
        return "5m"
    return "1h"


def _parse_levels(side: Any) -> list[OrderBookLevel]:
    out: list[OrderBookLevel] = []
    if not side:
        return out
    for row in side:
        if isinstance(row, dict):
            px = float(row.get("price") or row.get("p") or 0)
            sz = float(row.get("size") or row.get("s") or 0)
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            px, sz = float(row[0]), float(row[1])
        else:
            continue
        if px > 0 and sz > 0:
            out.append(OrderBookLevel(price=px, size=sz))
    return out


class PolymarketClient:
    """Gamma discovery + CLOB SDK orderbook. Auth optional for read paths."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        private_key: Optional[str] = None,
        timeout: float = 20.0,
    ):
        self.api_key = api_key or os.environ.get("POLYMARKET_API_KEY")
        self.private_key = private_key or os.environ.get("POLYMARKET_PK") or os.environ.get("PK")
        self.timeout = timeout
        self._clob = None

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _get_clob(self):
        if self._clob is not None:
            return self._clob
        try:
            from py_clob_client_v2 import ClobClient

            kwargs: dict[str, Any] = {"host": CLOB_HOST, "chain_id": CHAIN_ID}
            if self.private_key:
                kwargs["key"] = self.private_key
            self._clob = ClobClient(**kwargs)
            return self._clob
        except Exception as exc:  # noqa: BLE001
            logger.warning("py-clob-client-v2 unavailable (%s); HTTP fallback", exc)
            self._clob = False
            return None

    # ── Discovery (Gamma) ───────────────────────────────────────────────────

    def list_candidate_markets(self, limit: int = 50) -> list[MarketCandidate]:
        """Fetch active markets via Gamma. Raises on hard failure."""
        url = f"{GAMMA_HOST}/markets"
        params = {"limit": limit, "active": "true", "closed": "false"}
        with httpx.Client(timeout=self.timeout, headers=self._headers()) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        hour = datetime.now(timezone.utc).hour
        out: list[MarketCandidate] = []
        rows = data if isinstance(data, list) else data.get("data", data.get("markets", []))
        for row in rows[:limit]:
            try:
                out.append(self._to_candidate(row, hour))
            except Exception as exc:  # noqa: BLE001
                logger.debug("skip market row: %s", exc)
        if not out:
            raise RuntimeError("no markets returned from Polymarket Gamma")
        return out

    def get_market_by_slug(self, slug: str) -> Optional[MarketCandidate]:
        """Fetch a single market by exact slug (Gamma)."""
        url = f"{GAMMA_HOST}/markets"
        with httpx.Client(timeout=self.timeout, headers=self._headers()) as client:
            resp = client.get(url, params={"slug": slug})
            resp.raise_for_status()
            data = resp.json()
        rows = data if isinstance(data, list) else [data] if data else []
        if not rows:
            return None
        hour = datetime.now(timezone.utc).hour
        try:
            return self._to_candidate(rows[0], hour)
        except Exception as exc:  # noqa: BLE001
            logger.debug("slug %s parse failed: %s", slug, exc)
            return None

    def list_scoped_btc_updown_markets(self) -> list[MarketCandidate]:
        """Scoped discovery — respects MARKET_FILTER (BTC/ETH/SOL lanes).

        Kept name for backward compatibility; prefer ``list_scoped_updown_markets``.
        """
        return self.list_scoped_updown_markets()

    def list_scoped_updown_markets(self) -> list[MarketCandidate]:
        """Active windows for the instance MARKET_FILTER (one per series)."""
        from hermes.market_scope import (
            active_filter_keys,
            all_discovery_slugs,
            filter_specs,
            parse_slug,
            scope_enabled,
        )

        if not scope_enabled():
            return self.list_crypto_updown_markets(limit=40)

        specs = filter_specs()
        by_series: dict[str, MarketCandidate] = {}
        for slug in all_discovery_slugs():
            sm = parse_slug(slug)
            if not sm:
                continue
            if sm.series in by_series:
                continue
            try:
                c = self.get_market_by_slug(slug)
            except Exception as exc:  # noqa: BLE001
                logger.debug("fetch %s failed: %s", slug, exc)
                continue
            if c is None:
                continue
            if c.yes_price <= 0.02 or c.yes_price >= 0.98:
                continue
            from hermes.market_scope import is_window_tradeable

            if not is_window_tradeable(slug):
                logger.debug("skip expired/untradeable slug %s", slug)
                continue
            asset_u = sm.asset.upper()
            c.timeframe = sm.timeframe
            c.raw = {
                **(c.raw or {}),
                "timeframe": sm.timeframe,
                "asset": asset_u,
                "scoped_series": sm.series,
                "scoped_slug": sm.slug,
                "market_filter": sm.filter_key,
            }
            tags = list(c.tags)
            for t in (asset_u, f"tf:{sm.timeframe}", "scoped", sm.filter_key):
                if t and t not in tags:
                    tags.append(t)
            c.tags = tags
            by_series[sm.series] = c

        # Stable order following active filter keys
        ordered: list[MarketCandidate] = []
        for key in active_filter_keys():
            spec = specs.get(key)
            if not spec:
                continue
            if spec.series in by_series:
                ordered.append(by_series[spec.series])
        logger.info(
            "scoped discovery: %d markets %s (filters=%s)",
            len(ordered),
            [c.slug for c in ordered],
            active_filter_keys(),
        )
        return ordered

    def list_crypto_updown_markets(self, limit: int = 40) -> list[MarketCandidate]:
        """Scoped mode: MARKET_FILTER lanes. Legacy mode: BTC/ETH preference."""
        from hermes.market_scope import scope_enabled

        if scope_enabled():
            return self.list_scoped_updown_markets()
        all_m = self.list_candidate_markets(limit=max(limit * 3, 80))
        scored: list[tuple[int, MarketCandidate]] = []
        for c in all_m:
            blob = f"{c.slug} {c.question}".lower()
            score = 0
            if "btc" in blob or "bitcoin" in blob:
                score += 3
            if "eth" in blob or "ethereum" in blob:
                score += 3
            if "sol" in blob or "solana" in blob:
                score += 3
            if any(x in blob for x in ("updown", "up or down", "up-down", "5m", "15m")):
                score += 2
            if score:
                scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:limit]] or all_m[:limit]

    def _to_candidate(self, row: dict[str, Any], hour: int) -> MarketCandidate:
        prices = row.get("outcomePrices") or row.get("outcome_prices") or ["0.5", "0.5"]
        if isinstance(prices, str):
            prices = json.loads(prices)
        yes = float(prices[0]) if prices else 0.5
        no = float(prices[1]) if len(prices) > 1 else 1.0 - yes
        liq = float(row.get("liquidity") or row.get("liquidityNum") or 0)
        vol = float(row.get("volume24hr") or row.get("volume_24h") or row.get("volume") or 0)
        spread = abs(yes + no - 1.0) * 10_000 / 2 + 50
        slug = str(row.get("slug") or row.get("id") or "")
        question = str(row.get("question") or row.get("title") or "")
        timeframe = infer_timeframe(slug, question)
        # token ids for CLOB
        tokens = row.get("clobTokenIds") or row.get("clob_token_ids") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except json.JSONDecodeError:
                tokens = []
        tags = list(row.get("tags") or [])
        if timeframe not in tags:
            tags.append(f"tf:{timeframe}")
        return MarketCandidate(
            market_id=str(row.get("id") or row.get("conditionId") or slug),
            slug=slug,
            question=question,
            yes_price=yes,
            no_price=no,
            volume_24h=vol,
            liquidity=liq,
            spread_bps=spread,
            regime=Regime.UNKNOWN,
            hourly_bucket=hour,
            tags=tags,
            raw={
                "source": "polymarket_gamma",
                "id": row.get("id"),
                "conditionId": row.get("conditionId"),
                "clob_token_ids": tokens,
                "timeframe": timeframe,
                "yes_token_id": tokens[0] if tokens else None,
                "no_token_id": tokens[1] if len(tokens) > 1 else None,
                "endDate": row.get("endDate") or row.get("end_date"),
                "active": row.get("active"),
                "closed": row.get("closed"),
            },
        )

    # ── Orderbook (CLOB SDK / HTTP) ─────────────────────────────────────────

    def get_orderbook(self, token_id: str) -> OrderBookSnapshot:
        """Fetch L2 book for a CLOB token. Prefers official SDK."""
        clob = self._get_clob()
        if clob:
            try:
                book = clob.get_order_book(token_id)
                return self._normalize_book(token_id, book)
            except Exception as exc:  # noqa: BLE001
                logger.debug("SDK get_order_book failed: %s", exc)
        # HTTP fallback
        url = f"{CLOB_HOST}/book"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, params={"token_id": token_id})
            resp.raise_for_status()
            return self._normalize_book(token_id, resp.json())

    def _normalize_book(self, token_id: str, book: Any) -> OrderBookSnapshot:
        if hasattr(book, "bids") or hasattr(book, "asks"):
            bids_raw = getattr(book, "bids", None) or []
            asks_raw = getattr(book, "asks", None) or []
            # SDK may use objects with .price/.size
            def _lvl(x: Any) -> dict:
                if isinstance(x, dict):
                    return x
                return {"price": getattr(x, "price", 0), "size": getattr(x, "size", 0)}

            bids_raw = [_lvl(x) for x in bids_raw]
            asks_raw = [_lvl(x) for x in asks_raw]
            source = "clob_sdk"
        elif isinstance(book, dict):
            bids_raw = book.get("bids") or book.get("buys") or []
            asks_raw = book.get("asks") or book.get("sells") or []
            source = "clob_http"
        else:
            bids_raw, asks_raw, source = [], [], "unknown"

        bids = sorted(_parse_levels(bids_raw), key=lambda x: -x.price)
        asks = sorted(_parse_levels(asks_raw), key=lambda x: x.price)
        mid = None
        spread_bps = 0.0
        if bids and asks:
            mid = (bids[0].price + asks[0].price) / 2.0
            if mid > 0:
                spread_bps = (asks[0].price - bids[0].price) / mid * 10_000
        elif bids:
            mid = bids[0].price
        elif asks:
            mid = asks[0].price
        return OrderBookSnapshot(
            token_id=token_id,
            bids=bids,
            asks=asks,
            mid=mid,
            spread_bps=spread_bps,
            source=source,
        )

    def get_midpoint(self, token_id: str) -> Optional[float]:
        clob = self._get_clob()
        if clob:
            try:
                mid = clob.get_midpoint(token_id)
                if isinstance(mid, dict):
                    return float(mid.get("mid") or mid.get("midpoint") or 0) or None
                return float(mid)
            except Exception:  # noqa: BLE001
                pass
        book = self.get_orderbook(token_id)
        return book.mid

    def simulate_buy_vwap(self, token_id: str, size_usd: float) -> tuple[float, float]:
        """Walk the ask side for a paper BUY. Returns (vwap, slippage_bps vs mid)."""
        book = self.get_orderbook(token_id)
        if not book.asks or not book.mid:
            return 0.5, 50.0
        remaining = size_usd
        cost = 0.0
        shares = 0.0
        for lvl in book.asks:
            level_notional = lvl.price * lvl.size
            take = min(remaining, level_notional)
            sh = take / lvl.price if lvl.price else 0
            cost += take
            shares += sh
            remaining -= take
            if remaining <= 1e-9:
                break
        if shares <= 0:
            return book.asks[0].price, 100.0
        vwap = cost / shares
        slip = (vwap - book.mid) / book.mid * 10_000 if book.mid else 0.0
        return float(vwap), float(max(0.0, slip))

    def simulate_sell_vwap(self, token_id: str, size_usd: float) -> tuple[float, float]:
        """Walk the bid side for a paper SELL (buying NO ≈ selling YES semantics)."""
        book = self.get_orderbook(token_id)
        if not book.bids or not book.mid:
            return 0.5, 50.0
        remaining = size_usd
        proceeds = 0.0
        shares = 0.0
        for lvl in book.bids:
            level_notional = lvl.price * lvl.size
            take = min(remaining, level_notional)
            sh = take / lvl.price if lvl.price else 0
            proceeds += take
            shares += sh
            remaining -= take
            if remaining <= 1e-9:
                break
        if shares <= 0:
            return book.bids[0].price, 100.0
        vwap = proceeds / shares
        slip = (book.mid - vwap) / book.mid * 10_000 if book.mid else 0.0
        return float(vwap), float(max(0.0, slip))
