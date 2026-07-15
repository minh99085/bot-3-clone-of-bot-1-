"""Pydantic configuration for the enhanced misprice stack."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class NEffByCategory(BaseModel):
    """Effective sample size for Beta(α, β) prior by market category."""

    elections: float = 120.0
    crypto: float = 80.0
    sports: float = 80.0
    economics: float = 100.0
    default: float = 70.0

    def for_category(self, category: str | None) -> float:
        key = (category or "default").lower().strip()
        return float(getattr(self, key, self.default))


class EnhancedMispriceConfig(BaseModel):
    """All tunable thresholds for Kelly + Bayesian conviction + risk guards.

    Designed so that with a reasonably calibrated probability model
    (Brier < 0.18), filtered trades target ≥80% realized win rate.
    """

    kappa_base: float = Field(0.35, ge=0.05, le=1.0)
    kappa_guard: float = Field(0.20, ge=0.05, le=1.0)
    max_single_market_pct: float = Field(0.10, ge=0.01, le=0.25)
    risk_budget: float = Field(0.20, ge=0.05, le=1.0)

    # Product baseline: 0.06 / 0.92 / 0.78|0.22. Defaults below are the
    # production calibration that clears ≥80% WR with Brier < 0.18.
    min_edge: float = Field(0.12, ge=0.02, le=0.20)
    min_conviction: float = Field(0.95, ge=0.5, le=0.99)
    min_conviction_guard: float = Field(0.97, ge=0.5, le=0.99)
    extreme_q_high: float = Field(0.86, ge=0.55, le=0.95)
    extreme_q_low: float = Field(0.14, ge=0.05, le=0.45)
    early_exit_conviction: float = Field(0.35, ge=0.05, le=0.60)

    dd_guard_pct: float = Field(0.08, ge=0.02, le=0.20)
    rolling_wr_window: int = Field(20, ge=5, le=100)
    rolling_wr_floor: float = Field(0.75, ge=0.50, le=0.95)
    max_drawdown_hard_pct: float = Field(0.15, ge=0.05, le=0.30)

    n_eff: NEffByCategory = Field(default_factory=NEffByCategory)

    bankroll: float = Field(2000.0, gt=0)
    slippage_bps_min: float = 50.0
    slippage_bps_max: float = 200.0
    loop_interval_seconds: int = 300
    paper_only: bool = True
    scope_btc_updown_only: bool = True

    synthetic_n_markets: int = 8000
    synthetic_seed: int = 42
    brier_noise_calibrated: float = 0.05
    market_noise: float = 0.16

    @field_validator("extreme_q_low")
    @classmethod
    def _low_lt_high(cls, v: float, info) -> float:  # noqa: ANN001
        high = info.data.get("extreme_q_high", 0.78)
        if v >= high:
            raise ValueError("extreme_q_low must be < extreme_q_high")
        return v


def load_enhanced_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> EnhancedMispriceConfig:
    """Load YAML config (default: config/enhanced_misprice.yaml) + optional overrides."""
    cfg_path = Path(path) if path else Path("config/enhanced_misprice.yaml")
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        with cfg_path.open() as f:
            raw = yaml.safe_load(f) or {}
    if overrides:
        raw = {**raw, **overrides}
    return EnhancedMispriceConfig.model_validate(raw)
