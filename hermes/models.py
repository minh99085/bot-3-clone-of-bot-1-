"""Shared domain models for Hermes v2.

Every stage of the loop (discovery → signal → verify → execute → lesson)
hands off typed objects so parquet/JSON persistence stays schema-stable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    uid = uuid4().hex[:12]
    return f"{prefix}{uid}" if prefix else uid


class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    MEAN_REVERT = "mean_revert"
    HIGH_VOL = "high_vol"
    LOW_VOL = "low_vol"
    UNKNOWN = "unknown"


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    YES = "YES"
    NO = "NO"


class EntryMode(str, Enum):
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    NEWS_SHOCK = "news_shock"
    LIQUIDITY_SWEEP = "liquidity_sweep"
    OSMANI_LANE = "osmani_lane"  # gated until WR>65% + positive EV
    GROK_SIGNAL = "grok_signal"
    TV_SIGNAL = "tv_signal"
    MISPRICING = "mispricing"  # CEX↔Polymarket dislocation (Option D)


class ConfidenceTier(str, Enum):
    A = "A"  # highest conviction
    B = "B"
    C = "C"  # normally rejected by verifier
    D = "D"  # never trade


class LaneStatus(str, Enum):
    ACTIVE = "active"
    GATED = "gated"
    KILLED = "killed"
    PAPER_ONLY = "paper_only"


class SubStrategyAction(str, Enum):
    """Ruuj Ch.5 cut/reduce actions — separate losing from model-broken."""

    HOLD = "hold"
    REDUCE = "reduce"
    CUT = "cut"
    BOOST = "boost"  # rare: only when confidence rising + diversifying


class VerifierDecision(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    DEFER = "DEFER"  # human inbox


class MarketCandidate(BaseModel):
    market_id: str
    slug: str
    question: str
    end_date: Optional[datetime] = None
    yes_price: float
    no_price: float
    volume_24h: float = 0.0
    liquidity: float = 0.0
    spread_bps: float = 0.0
    regime: Regime = Regime.UNKNOWN
    hourly_bucket: int = Field(ge=0, le=23)
    timeframe: str = "1h"  # 5m | 15m | 1h | daily
    tags: list[str] = Field(default_factory=list)
    discovered_at: datetime = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("yes_price", "no_price")
    @classmethod
    def price_bounds(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"price must be in [0,1], got {v}")
        return v


class EdgeBucket(BaseModel):
    """Historical performance cell used by the verifier."""

    regime: Regime
    hourly_bucket: int
    entry_mode: EntryMode
    confidence_tier: ConfidenceTier
    direction_bias: str = "DOWN"  # explicit DOWN bias default
    sample_n: int = 0
    win_rate: float = 0.0
    avg_edge: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    avoid: bool = False
    exploit: bool = False
    notes: str = ""


class Signal(BaseModel):
    signal_id: str = Field(default_factory=lambda: new_id("sig_"))
    market_id: str
    slug: str
    question: str
    direction: Direction
    entry_mode: EntryMode
    confidence_tier: ConfidenceTier
    conviction: float = Field(ge=0.0, le=1.0)
    fair_value: float = Field(ge=0.0, le=1.0)
    market_price: float = Field(ge=0.0, le=1.0)
    expected_edge: float  # fair_value - market_price (signed by direction)
    live_ev: float = 0.0  # after fees + slippage
    regime: Regime
    hourly_bucket: int
    size_usd_suggested: float = 0.0
    entry_vwap_target: Optional[float] = None
    pre_entry_stability_ok: bool = False
    rationale: str = ""
    alpha_rules_fired: list[str] = Field(default_factory=list)
    avoid_bucket_hit: bool = False
    # Portfolio / sub-strategy fields (Ruuj allocation layer)
    market_series: str = "unknown"  # e.g. btc_updown, eth_updown
    substrategy_id: str = ""  # market_series|mode|regime|hN
    allocation_weight: float = 0.0  # proposed portfolio weight [0,1]
    allocation_usd: float = 0.0  # proposed sized USD after HRP/BL
    diversification_contrib: float = 0.0
    view_confidence: float = 0.0  # Black-Litterman view strength
    # Pre-trade sizing (Handoff analysis)
    size_pct_recommended: float = 0.0
    pretrade_skip: bool = False
    pretrade_reasons: list[str] = Field(default_factory=list)
    pretrade_analysis_id: str = ""
    # Chainlink / hybrid data fields
    timeframe: str = "1h"
    oracle_price: Optional[float] = None
    oracle_source: str = ""
    oracle_alignment: float = 0.5
    oracle_stale: bool = False
    clob_token_id: Optional[str] = None
    generated_at: datetime = Field(default_factory=utc_now)
    generator_model: str = "alpha-research-agent"
    meta: dict[str, Any] = Field(default_factory=dict)


class SubStrategyConfidence(BaseModel):
    """Internal confidence per return source — drives HOLD/REDUCE/CUT."""

    substrategy_id: str
    market_series: str
    entry_mode: EntryMode
    regime: Regime
    hourly_bucket: int
    sample_n: int = 0
    rolling_ev: float = 0.0  # after cost
    rolling_wr: float = 0.0
    wr_trend: float = 0.0  # positive = improving
    ev_trend: float = 0.0
    regime_stability: float = 1.0  # 1 = stable
    brier_score: float = 0.25  # lower better; 0.25 = uninformative
    internal_confidence: float = 0.5  # composite [0,1]
    action: SubStrategyAction = SubStrategyAction.HOLD
    weight_cap: float = 0.25  # max portfolio weight allowed
    currently_losing: bool = False  # PnL streak — NOT the same as model broken
    model_broken: bool = False  # reason-for-working degraded
    notes: str = ""
    updated_at: datetime = Field(default_factory=utc_now)


class AllocationProposal(BaseModel):
    """Handoff artifact: risk-parity base + BL views → sized opportunities."""

    proposal_id: str = Field(default_factory=lambda: new_id("alc_"))
    method: str = "hrp_edge_bl"  # hrp | edge_rp | hrp_edge_bl
    capital_usd: float
    weights: dict[str, float] = Field(default_factory=dict)  # substrategy_id -> w
    signal_sizes_usd: dict[str, float] = Field(default_factory=dict)  # signal_id -> $
    diversification_ratio: float = 1.0
    portfolio_vol_proxy: float = 0.0
    concentration_hhi: float = 0.0  # Herfindahl; lower = more diversified
    cut_list: list[str] = Field(default_factory=list)
    reduce_list: list[str] = Field(default_factory=list)
    view_tilts: dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    notes: str = ""


class PreTradeAnalysis(BaseModel):
    """Structured pre-trade sizing decision (Handoff → Verifier)."""

    analysis_id: str = Field(default_factory=lambda: new_id("pta_"))
    signal_id: str
    substrategy_id: str
    bankroll_usd: float
    recommended_size_pct: float = 0.0  # % of bankroll; 0 = skip
    recommended_size_usd: float = 0.0
    skip: bool = False
    live_ev: float = 0.0
    sleeve_wr: float = 0.0
    sleeve_ev: float = 0.0
    sleeve_n: int = 0
    portfolio_div_before: float = 1.0
    portfolio_div_after: float = 1.0
    concentration_after: float = 0.0
    allocation_weight: float = 0.0
    lessons_applied: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    oracle_alignment: float = 0.5
    # Option D — mispricing + bandit
    mispricing_active: bool = False
    mispricing_dislocation: float = 0.0
    bandit_arm: str = ""
    bandit_context: str = ""
    entry_source: str = "baseline"  # mispricing | baseline
    created_at: datetime = Field(default_factory=utc_now)


class PortfolioSnapshot(BaseModel):
    taken_at: datetime = Field(default_factory=utc_now)
    capital_usd: float
    n_substrategies_active: int = 0
    n_cut: int = 0
    n_reduce: int = 0
    diversification_ratio: float = 1.0
    concentration_hhi: float = 0.0
    open_exposure_usd: float = 0.0
    top_weights: dict[str, float] = Field(default_factory=dict)


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str
    weight: float = 1.0


class VerificationReport(BaseModel):
    report_id: str = Field(default_factory=lambda: new_id("vrf_"))
    signal_id: str
    decision: VerifierDecision
    checks: list[CheckResult] = Field(default_factory=list)
    score: float = 0.0
    rejection_reasons: list[str] = Field(default_factory=list)
    sized_usd: float = 0.0
    allocation_weight: float = 0.0
    allocation_approved: bool = False
    substrategy_id: str = ""
    substrategy_action: str = "hold"
    verifier_model: str = "verifier-strong"
    verified_at: datetime = Field(default_factory=utc_now)
    notes: str = ""

    @property
    def passed(self) -> bool:
        return self.decision == VerifierDecision.PASS


class OrderIntent(BaseModel):
    intent_id: str = Field(default_factory=lambda: new_id("ord_"))
    signal_id: str
    market_id: str
    direction: Direction
    size_usd: float
    limit_price: float
    entry_mode: EntryMode
    paper: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class Fill(BaseModel):
    fill_id: str = Field(default_factory=lambda: new_id("fil_"))
    intent_id: str
    signal_id: str
    market_id: str
    direction: Direction
    size_usd: float
    fill_price: float
    fees_usd: float = 0.0
    slippage_bps: float = 0.0
    paper: bool = True
    filled_at: datetime = Field(default_factory=utc_now)


class Position(BaseModel):
    position_id: str = Field(default_factory=lambda: new_id("pos_"))
    signal_id: str
    market_id: str
    direction: Direction
    size_usd: float
    entry_price: float
    unrealized_pnl: float = 0.0
    opened_at: datetime = Field(default_factory=utc_now)
    paper: bool = True


class Settlement(BaseModel):
    settlement_id: str = Field(default_factory=lambda: new_id("stl_"))
    position_id: str
    signal_id: str
    market_id: str
    direction: Direction
    entry_price: float
    exit_price: float
    size_usd: float
    pnl_usd: float
    won: bool
    regime: Regime
    hourly_bucket: int
    entry_mode: EntryMode
    confidence_tier: ConfidenceTier
    market_series: str = "unknown"
    substrategy_id: str = ""
    slug: str = ""
    timeframe: str = "1h"
    settled_at: datetime = Field(default_factory=utc_now)
    paper: bool = True
    notes: str = ""


class Lesson(BaseModel):
    lesson_id: str = Field(default_factory=lambda: new_id("les_"))
    created_at: datetime = Field(default_factory=utc_now)
    source: str  # settlement | rejection | near_miss | risk_halt
    severity: str = "medium"  # low | medium | high | critical
    rule: str  # actionable imperative
    evidence: str
    applies_to: list[str] = Field(default_factory=list)  # buckets/modes/regimes
    promote_to: Optional[str] = None  # ALPHA_RESEARCH_SKILL | SKILL | None
    retired: bool = False
    retired_at: Optional[datetime] = None
    retire_evidence: str = ""


class RiskSnapshot(BaseModel):
    taken_at: datetime = Field(default_factory=utc_now)
    capital_usd: float
    open_exposure_usd: float
    daily_pnl_usd: float
    rolling_wr_20: float
    rolling_pf_20: float
    max_drawdown_pct: float
    consecutive_losses: int
    circuit_breaker_tripped: bool = False
    trip_reason: str = ""
    pause_loop: bool = False


class LoopTurnResult(BaseModel):
    turn_id: str = Field(default_factory=lambda: new_id("trn_"))
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = None
    candidates_found: int = 0
    signals_generated: int = 0
    signals_passed: int = 0
    signals_rejected: int = 0
    orders_sent: int = 0
    lessons_written: int = 0
    deferred_to_inbox: int = 0
    paused: bool = False
    pause_reason: str = ""
    summary: str = ""
