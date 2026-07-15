"""CEX data connector — leading indicators (BTC/ETH volume, funding, etc.)."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class CexClient:
    """Thin wrapper; prefer MCP server in production for Binance/Bybit/Coinbase."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url or os.environ.get(
            "CEX_API_BASE", "https://api.binance.com"
        )

    def btc_usdt_ticker(self) -> dict[str, Any]:
        url = f"{self.base_url}/api/v3/ticker/24hr"
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params={"symbol": "BTCUSDT"})
            resp.raise_for_status()
            return resp.json()

    def leading_features(self) -> dict[str, float]:
        """Features discovery/signal layers can consume."""
        try:
            t = self.btc_usdt_ticker()
            return {
                "btc_change_pct": float(t.get("priceChangePercent", 0)),
                "btc_quote_volume": float(t.get("quoteVolume", 0)),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("cex features unavailable: %s", exc)
            return {"btc_change_pct": 0.0, "btc_quote_volume": 0.0}
