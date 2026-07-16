"""Pydantic configuration for the enhanced misprice stack."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

FilterMode = Literal["strict", "moderate", "aggressive"]

# Mode presets control entry filters + sizing. Tuned on synthetic backtests
# (seed=42, 5k markets + Monte Carlo) so that:
#   strict    → max WR, fewest trades
#   moderate  → more trades, WR safely above 85% (MC p5 ≥ 85%)
#   aggressive→ highest frequency, still aiming ~80%+ WR
#
# min_edge stays at 0.12 across modes: lowering it admits early losers that
# trip the 15% hard-DD lockout and starve the rest of the run.
MODE_PRESETS: dict[str, dict[str, Any]] = {
    "strict": {
        "min_edge": 0.12,
        "min_conviction": 0.95,
        "min_conviction_guard": 0.97,
        "extreme_q_high": 0.88,
        "extreme_q_low": 0.12,
        "kappa_base": 0.35,
        "max_single_market_pct": 0.10,
        "risk_budget": 0.20,
        "n_eff_crypto": 80.0,
    },
    "moderate": {
        # Live paper: real cex_implied_up as q (no artificial 0.97/0.03 push).
        # Wider extreme_q band + slightly looser edge/conviction so genuine
        # CEX-implied probs can clear gates without fake q inflation.
        "min_edge": 0.085,
        "min_conviction": 0.88,
        "min_conviction_guard": 0.94,
        "extreme_q_high": 0.80,
        "extreme_q_low": 0.20,
        "kappa_base": 0.40,
        "max_single_market_pct": 0.09,
        "risk_budget": 0.20,
        "n_eff_crypto": 80.0,
    },
    "aggressive": {
        # Highest frequency of the three; WR typically ~80–83% on synthetic.
        "min_edge": 0.12,
        "min_conviction": 0.93,
        "min_conviction_guard": 0.95,
        "extreme_q_high": 0.85,
        "extreme_q_low": 0.15,
        "kappa_base": 0.30,
        "max_single_market_pct": 0.08,
        "risk_budget": 0.18,
        "n_eff_crypto": 80.0,
    },
}


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

    # Profile: applies MODE_PRESETS on load (see load_enhanced_config).
    mode: FilterMode = "strict"

    kappa_base: float = Field(0.35, ge=0.05, le=1.0)
    kappa_guard: float = Field(0.20, ge=0.05, le=1.0)
    max_single_market_pct: float = Field(0.10, ge=0.01, le=0.25)
    risk_budget: float = Field(0.20, ge=0.05, le=1.0)

    # Product baseline: 0.06 / 0.92 / 0.78|0.22. Defaults below match
    # the ``strict`` mode preset.
    min_edge: float = Field(0.12, ge=0.02, le=0.20)
    min_conviction: float = Field(0.95, ge=0.5, le=0.99)
    min_conviction_guard: float = Field(0.97, ge=0.5, le=0.99)
    extreme_q_high: float = Field(0.88, ge=0.55, le=0.95)
    extreme_q_low: float = Field(0.12, ge=0.05, le=0.45)
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

    # Rich synthetic generator (production backtest suite)
    synthetic_n_min: int = 5000
    synthetic_n_max: int = 20000
    decision_fracs: list[float] = Field(default_factory=lambda: [0.30, 0.60, 0.85])
    days_to_res_choices: list[float] = Field(
        default_factory=lambda: [3.0, 14.0, 45.0, 120.0]
    )
    days_to_res_weights: list[float] = Field(
        default_factory=lambda: [0.25, 0.35, 0.25, 0.15]
    )
    block_size: int = 8  # correlated markets per thematic block
    block_corr: float = Field(0.72, ge=0.0, le=0.95)
    extreme_mass: float = Field(0.55, ge=0.1, le=0.9)
    categories: list[str] = Field(
        default_factory=lambda: ["crypto", "elections", "sports", "economics"]
    )

    # Monte Carlo / tuning defaults
    monte_carlo_runs: int = 100
    tune_trials: int = 40

    @field_validator("extreme_q_low")
    @classmethod
    def _low_lt_high(cls, v: float, info) -> float:  # noqa: ANN001
        high = info.data.get("extreme_q_high", 0.78)
        if v >= high:
            raise ValueError("extreme_q_low must be < extreme_q_high")
        return v

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        key = str(v).strip().lower()
        if key not in MODE_PRESETS:
            raise ValueError(
                f"mode must be one of {sorted(MODE_PRESETS)} (got {v!r})"
            )
        return key


def apply_mode_preset(
    raw: dict[str, Any],
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Merge MODE_PRESETS[mode] into a raw config dict.

    Mode-controlled keys always win over stale top-level YAML values so
    switching ``mode:`` alone is enough. Nested ``n_eff.crypto`` is updated
    when the preset defines ``n_eff_crypto``.
    """
    out = dict(raw)
    chosen = (mode or out.get("mode") or "strict")
    chosen = str(chosen).strip().lower()
    if chosen not in MODE_PRESETS:
        raise ValueError(f"Unknown filter mode {chosen!r}; expected {sorted(MODE_PRESETS)}")
    out["mode"] = chosen
    preset = MODE_PRESETS[chosen]
    for key, value in preset.items():
        if key == "n_eff_crypto":
            n_eff = dict(out.get("n_eff") or {})
            n_eff["crypto"] = float(value)
            out["n_eff"] = n_eff
        else:
            out[key] = value
    return out


def load_enhanced_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    mode: str | None = None,
) -> EnhancedMispriceConfig:
    """Load YAML config (default: config/enhanced_misprice.yaml) + optional overrides.

    Parameters
    ----------
    mode:
        If set, forces that filter profile (strict / moderate / aggressive)
        after YAML load. Otherwise uses ``mode:`` from the YAML (default strict).
    """
    cfg_path = Path(path) if path else Path("config/enhanced_misprice.yaml")
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        with cfg_path.open() as f:
            raw = yaml.safe_load(f) or {}
    if overrides:
        # Allow overrides to set mode before preset application
        raw = {**raw, **overrides}
    raw = apply_mode_preset(raw, mode=mode)
    # Re-apply non-mode overrides so callers can fine-tune after the preset
    if overrides:
        fine = {k: v for k, v in overrides.items() if k != "mode"}
        if fine:
            if "n_eff" in fine and isinstance(fine["n_eff"], dict):
                merged_n = dict(raw.get("n_eff") or {})
                merged_n.update(fine["n_eff"])
                fine = {**fine, "n_eff": merged_n}
            raw = {**raw, **fine}
    return EnhancedMispriceConfig.model_validate(raw)
