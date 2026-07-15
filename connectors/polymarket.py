"""Polymarket MCP-style connector.

Production: wire to Gamma/CLOB APIs or an MCP server.
Paper: returns empty and lets discovery fall back to synthetics, or
fetches public markets when POLYMARKET_API available.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from hermes.models import MarketCandidate, Regime

logger = logging.getLogger(__name__)

GAMMA_HOST = os.environ.get("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")


class PolymarketClient:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 20.0):
        self.api_key = api_key or os.environ.get("POLYMARKET_API_KEY")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def list_candidate_markets(self, limit: int = 50) -> list[MarketCandidate]:
        """Fetch active markets. Raises on hard failure so discovery can fallback."""
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
            raise RuntimeError("no markets returned from Polymarket")
        return out

    def _to_candidate(self, row: dict[str, Any], hour: int) -> MarketCandidate:
        # Gamma schema varies; be defensive
        prices = row.get("outcomePrices") or row.get("outcome_prices") or ["0.5", "0.5"]
        if isinstance(prices, str):
            import json

            prices = json.loads(prices)
        yes = float(prices[0]) if prices else 0.5
        no = float(prices[1]) if len(prices) > 1 else 1.0 - yes
        liq = float(row.get("liquidity") or row.get("liquidityNum") or 0)
        vol = float(row.get("volume24hr") or row.get("volume_24h") or row.get("volume") or 0)
        spread = abs(yes + no - 1.0) * 10_000 / 2 + 50  # rough proxy
        return MarketCandidate(
            market_id=str(row.get("id") or row.get("conditionId") or row.get("slug")),
            slug=str(row.get("slug") or row.get("id")),
            question=str(row.get("question") or row.get("title") or ""),
            yes_price=yes,
            no_price=no,
            volume_24h=vol,
            liquidity=liq,
            spread_bps=spread,
            regime=Regime.UNKNOWN,
            hourly_bucket=hour,
            tags=list(row.get("tags") or []),
            raw={"source": "polymarket", "id": row.get("id")},
        )

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        raise NotImplementedError("Wire CLOB orderbook via MCP in production")

    def get_positions(self, address: str) -> list[dict[str, Any]]:
        raise NotImplementedError("Wire positions via MCP in production")
