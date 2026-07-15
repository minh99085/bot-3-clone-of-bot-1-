"""Broker execution connector — paper first, live behind hard flags."""

from __future__ import annotations

import logging
import os
from typing import Optional

from hermes.models import Fill, OrderIntent

logger = logging.getLogger(__name__)


class BrokerClient:
    def __init__(self, paper: bool = True):
        self.paper = paper
        if not paper and os.environ.get("HERMES_LIVE") != "1":
            raise RuntimeError("Refusing live broker without HERMES_LIVE=1")

    def execute(self, intent: OrderIntent) -> Fill:
        if self.paper or intent.paper:
            return self._paper_fill(intent)
        return self._live_fill(intent)

    def _paper_fill(self, intent: OrderIntent) -> Fill:
        # Conservative adverse selection: 20 bps slippage
        slip = 0.002
        direction = intent.direction.value
        if direction in ("YES", "UP"):
            px = min(0.99, intent.limit_price + slip)
        else:
            px = max(0.01, intent.limit_price - slip)
        fees = intent.size_usd * 0.01
        logger.info(
            "PAPER FILL %s %s $%.2f @ %.4f",
            direction,
            intent.market_id,
            intent.size_usd,
            px,
        )
        return Fill(
            intent_id=intent.intent_id,
            signal_id=intent.signal_id,
            market_id=intent.market_id,
            direction=intent.direction,
            size_usd=intent.size_usd,
            fill_price=px,
            fees_usd=fees,
            slippage_bps=20.0,
            paper=True,
        )

    def _live_fill(self, intent: OrderIntent) -> Fill:
        # Placeholder — wire Polymarket CLOB / MCP broker here
        raise NotImplementedError(
            "Live execution requires connectors/polymarket CLOB + wallet secrets. "
            "Run paper mode until verifier WR >= 80% on settled trades."
        )
