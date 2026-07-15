"""Broker execution — paper fills from live CLOB orderbook + Chainlink context.

Paper mode walks the Polymarket book (py-clob-client-v2 / HTTP) for realistic
VWAP + slippage. Chainlink prices are logged as ground-truth context for
BTC/ETH markets (especially 5m/15m). Live posts require HERMES_LIVE=1 + PK.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from connectors.cex_realtime import get_asset_mid
from hermes.models import Fill, OrderIntent

logger = logging.getLogger(__name__)


class BrokerClient:
    def __init__(self, paper: bool = True):
        paper_only = os.environ.get("HERMES_PAPER_ONLY", "1").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if paper_only:
            paper = True
            os.environ["HERMES_LIVE"] = "0"
        self.paper = paper
        if not paper and os.environ.get("HERMES_LIVE") != "1":
            raise RuntimeError("Refusing live broker without HERMES_LIVE=1")

    def execute(self, intent: OrderIntent, *, token_id: Optional[str] = None, asset: Optional[str] = None) -> Fill:
        if self.paper or intent.paper:
            return self._paper_fill(intent, token_id=token_id, asset=asset)
        paper_only = os.environ.get("HERMES_PAPER_ONLY", "1").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if paper_only:
            raise RuntimeError("Live fills disabled in Hermes Paper (HERMES_PAPER_ONLY=1)")
        return self._live_fill(intent, token_id=token_id)

    def _paper_fill(
        self,
        intent: OrderIntent,
        *,
        token_id: Optional[str] = None,
        asset: Optional[str] = None,
    ) -> Fill:
        direction = intent.direction.value
        fill_price = intent.limit_price
        slip_bps = 20.0
        oracle_note = ""

        asset_u = (asset or "").upper()
        if asset_u in ("BTC", "ETH", "SOL"):
            try:
                if asset_u == "SOL":
                    mid = get_asset_mid("SOL", force_rest=True)
                    oracle_note = f" cex_sol={mid:.2f}" if mid > 0 else ""
                else:
                    from connectors.chainlink import ChainlinkClient

                    px = ChainlinkClient().get_price(asset_u)
                    oracle_note = f" cl={px.price_usd:.2f}@{px.source}"
            except Exception as exc:  # noqa: BLE001
                logger.debug("oracle context skipped: %s", exc)

        # Walk orderbook when token_id known
        if token_id:
            try:
                from connectors.polymarket import PolymarketClient

                pm = PolymarketClient()
                if direction in ("YES", "UP"):
                    fill_price, slip_bps = pm.simulate_buy_vwap(token_id, intent.size_usd)
                else:
                    # Buying NO token if available; else sell-side walk on YES as proxy
                    fill_price, slip_bps = pm.simulate_buy_vwap(token_id, intent.size_usd)
                # Respect limit: no fill worse than limit for paper aggressor
                if direction in ("YES", "UP"):
                    fill_price = min(0.99, max(fill_price, intent.limit_price))
                else:
                    fill_price = max(0.01, min(fill_price, intent.limit_price + 0.02))
            except Exception as exc:  # noqa: BLE001
                logger.debug("orderbook sim failed (%s); using limit+slip", exc)
                slip = 0.002
                if direction in ("YES", "UP"):
                    fill_price = min(0.99, intent.limit_price + slip)
                else:
                    fill_price = max(0.01, intent.limit_price - slip)
                slip_bps = 20.0
        else:
            slip = 0.002
            if direction in ("YES", "UP"):
                fill_price = min(0.99, intent.limit_price + slip)
            else:
                fill_price = max(0.01, intent.limit_price - slip)

        fees = intent.size_usd * 0.01
        logger.info(
            "PAPER FILL %s %s $%.2f @ %.4f slip=%.1fbps%s",
            direction,
            intent.market_id,
            intent.size_usd,
            fill_price,
            slip_bps,
            oracle_note,
        )
        return Fill(
            intent_id=intent.intent_id,
            signal_id=intent.signal_id,
            market_id=intent.market_id,
            direction=intent.direction,
            size_usd=intent.size_usd,
            fill_price=fill_price,
            fees_usd=fees,
            slippage_bps=float(slip_bps),
            paper=True,
        )

    def _live_fill(self, intent: OrderIntent, *, token_id: Optional[str] = None) -> Fill:
        if not token_id:
            raise NotImplementedError("Live fill requires clob token_id")
        pk = os.environ.get("POLYMARKET_PK") or os.environ.get("PK")
        if not pk:
            raise RuntimeError("POLYMARKET_PK required for live orders")
        try:
            from py_clob_client_v2 import ClobClient, OrderArgs, OrderType, Side
        except ImportError as exc:
            raise NotImplementedError("py-clob-client-v2 required for live") from exc

        host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
        client = ClobClient(host=host, chain_id=137, key=pk)
        creds = client.create_or_derive_api_key()
        client = ClobClient(host=host, chain_id=137, key=pk, creds=creds)
        side = Side.BUY
        size = intent.size_usd / max(intent.limit_price, 0.01)
        args = OrderArgs(
            token_id=token_id,
            price=float(intent.limit_price),
            size=float(size),
            side=side,
        )
        resp = client.create_and_post_order(args, order_type=OrderType.GTC)
        logger.info("LIVE ORDER posted: %s", resp)
        return Fill(
            intent_id=intent.intent_id,
            signal_id=intent.signal_id,
            market_id=intent.market_id,
            direction=intent.direction,
            size_usd=intent.size_usd,
            fill_price=float(intent.limit_price),
            fees_usd=intent.size_usd * 0.01,
            slippage_bps=0.0,
            paper=False,
        )
