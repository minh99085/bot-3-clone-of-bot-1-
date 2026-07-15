"""Paper trading simulator — realistic fills, no on-chain signing.

Slippage: uniform 0.5%–2% of price (adverse). Tracks cash, open positions,
and mark-to-market equity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import numpy as np

from models.config import EnhancedMispriceConfig, load_enhanced_config
from models.market import ClosedTrade, OpenPosition, Side, TradeOpportunity

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Fill:
    position_id: str
    market_id: str
    side: Side
    fill_price: float
    size_usd: float
    shares: float
    slippage_bps: float
    filled_at: datetime = field(default_factory=utc_now)


class PaperSimulator:
    """Cash-account paper broker with adverse slippage."""

    def __init__(
        self,
        config: Optional[EnhancedMispriceConfig] = None,
        *,
        seed: int = 7,
    ) -> None:
        self.cfg = config or load_enhanced_config()
        self.cash = float(self.cfg.bankroll)
        self.peak = self.cash
        self.open: dict[str, OpenPosition] = {}
        self.closed: list[ClosedTrade] = []
        self.fills: list[Fill] = []
        self.rng = np.random.default_rng(seed)
        assert self.cfg.paper_only, "PaperSimulator refuses non-paper config"

    @property
    def equity(self) -> float:
        return self.cash + sum(p.size_usd for p in self.open.values())

    def _slip_price(self, price: float, side: Side) -> tuple[float, float]:
        """Adverse slippage in [slippage_bps_min, slippage_bps_max]."""
        bps = float(
            self.rng.uniform(self.cfg.slippage_bps_min, self.cfg.slippage_bps_max)
        )
        mult = bps / 10_000.0
        if side in (Side.YES, Side.UP):
            # Buying YES → pay more
            px = min(0.99, price * (1.0 + mult))
        else:
            # Buying NO at (1-p_yes) already encoded in opp.p; pay more for NO
            px = min(0.99, price * (1.0 + mult))
        return float(px), bps

    def open_position(self, opp: TradeOpportunity) -> Optional[Fill]:
        if not self.cfg.paper_only:
            raise RuntimeError("Refusing live execution in PaperSimulator")
        if opp.size_usd <= 0 or not opp.passes_hard_filter:
            return None
        if opp.size_usd > self.cash:
            logger.info("skip fill: insufficient cash")
            return None

        fill_px, bps = self._slip_price(opp.p, opp.side)
        shares = opp.size_usd / max(fill_px, 1e-9)
        pid = f"pos_{uuid4().hex[:10]}"
        fill = Fill(
            position_id=pid,
            market_id=opp.market_id,
            side=opp.side,
            fill_price=fill_px,
            size_usd=opp.size_usd,
            shares=shares,
            slippage_bps=bps,
        )
        pos = OpenPosition(
            position_id=pid,
            market_id=opp.market_id,
            slug=opp.slug,
            side=opp.side,
            entry_price=fill_px,
            size_usd=opp.size_usd,
            shares=shares,
            q_at_entry=opp.q,
            conviction_at_entry=opp.conviction,
            risk_unit=opp.risk_unit,
            meta=dict(opp.meta or {}),
        )
        self.cash -= opp.size_usd
        self.open[pid] = pos
        self.fills.append(fill)
        self.peak = max(self.peak, self.equity)
        logger.debug(
            "PAPER FILL %s %s $%.2f @ %.3f slip=%.0fbps",
            opp.side.value,
            opp.market_id,
            opp.size_usd,
            fill_px,
            bps,
        )
        return fill

    def close_position(self, trade: ClosedTrade) -> None:
        self.open.pop(trade.position_id, None)
        # Stake returned + PnL (stake was deducted at open)
        self.cash += trade.size_usd + trade.pnl_usd
        self.closed.append(trade)
        self.peak = max(self.peak, self.equity)

    def mark_to_market(self, mids: dict[str, float]) -> float:
        total = self.cash
        for p in self.open.values():
            mid = mids.get(p.market_id)
            if mid is None:
                total += p.size_usd
            else:
                total += p.shares * mid
        return total
