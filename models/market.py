"""Market / opportunity / position pydantic models for enhanced misprice."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    YES = "YES"
    NO = "NO"
    UP = "UP"
    DOWN = "DOWN"


class MarketSnapshot(BaseModel):
    """Normalized live or synthetic market state."""

    market_id: str
    slug: str = ""
    question: str = ""
    category: str = "crypto"
    timeframe: str = "5m"
    p: float = Field(..., ge=0.0, le=1.0, description="Market implied P(YES/UP)")
    q: float = Field(..., ge=0.0, le=1.0, description="Model fair P(YES/UP)")
    liquidity_usd: float = 0.0
    volume_24h: float = 0.0
    seconds_to_resolution: float = 300.0
    true_q: Optional[float] = None  # backtest only
    resolved_yes: Optional[bool] = None  # backtest only
    meta: dict[str, Any] = Field(default_factory=dict)
    as_of: datetime = Field(default_factory=utc_now)


class TradeOpportunity(BaseModel):
    """Ranked, filter-passing trade candidate with Kelly size."""

    market_id: str
    slug: str = ""
    side: Side
    p: float  # price paid for the chosen side
    q: float  # model prob for YES/UP
    edge: float
    conviction: float
    conviction_score: float
    kelly_f_star: float
    kelly_f: float
    kappa: float
    size_usd: float
    risk_unit: float
    liquidity_score: float
    time_decay_factor: float
    passes_hard_filter: bool
    reasons: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class OpenPosition(BaseModel):
    """Paper / backtest open position."""

    position_id: str
    market_id: str
    slug: str = ""
    side: Side
    entry_price: float
    size_usd: float
    shares: float = 0.0
    q_at_entry: float
    conviction_at_entry: float
    risk_unit: float
    opened_at: datetime = Field(default_factory=utc_now)
    meta: dict[str, Any] = Field(default_factory=dict)


class ClosedTrade(BaseModel):
    """Resolved or early-exited trade for reporting."""

    position_id: str
    market_id: str
    side: Side
    entry_price: float
    exit_price: float
    size_usd: float
    pnl_usd: float
    won: bool
    conviction_at_entry: float
    edge_at_entry: float
    early_exit: bool = False
    closed_at: datetime = Field(default_factory=utc_now)
    meta: dict[str, Any] = Field(default_factory=dict)
