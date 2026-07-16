"""Pydantic / dataclass schemas for the autonomy stack.

Pseudocode contracts
--------------------
SettlementReward:
  reward = sigmoid(pnl_usd / size_usd) * 0.7 + (1 - brier_delta) * 0.3

ContextFeatures:
  vol_regime ∈ {low, mid, high}
  ttr_bucket ∈ {early, mid, late}
  liq_score ∈ [0,1]
  sentiment ∈ {bear, neutral, bull}
  category ∈ {crypto, …}
  family ∈ strategy family id

ModelCard:
  name, version, metrics{wr, dd, brier, n}, status∈{shadow,prod,retired}
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class VolRegime(str, Enum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"


class TTRBucket(str, Enum):
    EARLY = "early"
    MID = "mid"
    LATE = "late"


class Sentiment(str, Enum):
    BEAR = "bear"
    NEUTRAL = "neutral"
    BULL = "bull"


class ModelStatus(str, Enum):
    SHADOW = "shadow"
    PROD = "prod"
    RETIRED = "retired"
    ROLLED_BACK = "rolled_back"


class ContextFeatures(BaseModel):
    """Hierarchical bandit context."""

    vol_regime: VolRegime = VolRegime.MID
    ttr_bucket: TTRBucket = TTRBucket.MID
    liq_score: float = Field(0.5, ge=0.0, le=1.0)
    sentiment: Sentiment = Sentiment.NEUTRAL
    category: str = "crypto"
    timeframe: str = "5m"
    family: str = "mispricing"
    hour: int = Field(12, ge=0, le=23)
    dislocation: float = 0.0
    hurst: Optional[float] = None

    def family_key(self) -> str:
        return f"fam:{self.family}|{self.category}|{self.timeframe}"

    def leaf_key(self) -> str:
        return (
            f"{self.timeframe}|{self.vol_regime.value}|{self.ttr_bucket.value}|"
            f"{self.sentiment.value}|liq{int(self.liq_score * 4)}|h{self.hour // 4}"
        )


class SettlementReward(BaseModel):
    """Risk-adjusted reward for MCHB / CBPF updates."""

    pnl_usd: float
    size_usd: float
    won: bool
    brier: Optional[float] = None  # (q - y)^2 if available
    model_q: Optional[float] = None
    resolved_yes: Optional[bool] = None

    def as_unit_reward(self) -> float:
        """Map to [0,1] for Thompson / Dirichlet."""
        if self.size_usd <= 0:
            base = 1.0 if self.won else 0.0
        else:
            ret = self.pnl_usd / self.size_usd
            # tanh-ish soft map
            base = max(0.0, min(1.0, 0.5 + 0.5 * max(-1.0, min(1.0, ret * 2.0))))
        if self.brier is not None:
            # Lower Brier → boost reward
            cal = max(0.0, min(1.0, 1.0 - float(self.brier) / 0.25))
            return max(0.0, min(1.0, 0.70 * base + 0.30 * cal))
        return base


class AutonomyState(BaseModel):
    """Persisted controller state (per instance)."""

    n_resolved: int = 0
    rolling_wins: int = 0
    rolling_n: int = 0
    peak_equity: float = 2000.0
    equity: float = 2000.0
    disabled_families: list[str] = Field(default_factory=list)
    soft_kappa_scale: float = 1.0
    size_multiplier: float = 1.0
    last_eho_at: Optional[str] = None
    last_eho_n: int = 0
    last_cbpf_n: int = 0
    last_ingest_at: Optional[str] = None
    last_nightly_at: Optional[str] = None
    prod_model_version: Optional[str] = None
    shadow_model_version: Optional[str] = None
    shadow_trades: int = 0
    shadow_wins: int = 0
    last_rollback_at: Optional[str] = None
    last_promote_at: Optional[str] = None
    audit: list[dict[str, Any]] = Field(default_factory=list)
    mutable_params: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class ModelCard(BaseModel):
    name: str
    version: str
    status: ModelStatus = ModelStatus.SHADOW
    metrics: dict[str, float] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    promoted_at: Optional[datetime] = None
    notes: str = ""


class IngestBatch(BaseModel):
    source: str
    n_rows: int = 0
    path: str = ""
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = None
    ok: bool = True
    error: str = ""
