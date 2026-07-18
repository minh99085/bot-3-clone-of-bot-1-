"""Pydantic configuration for the enhanced misprice stack."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

FilterMode = Literal["strict", "strict_real", "moderate", "aggressive"]
ExtremeAnchor = Literal["q", "p", "either", "none"]

# Hermes Agent v3 — frozen production profile (real cex_implied_up as q).
# Gold standard: reports/full_backtest_vps_20260716_strict_real
#   89.7% WR · MC p5 85.3% · 919 trades · DD 8.0% · PF 3.05
# DO NOT loosen min_edge below 0.14 under real q — weaker buckets destroy WR.
#
# extreme_anchor=q + 0.85/0.15 keeps synthetic WR. Live desk sets
# live_real_q=True so mid CEX q uses extreme_p_* on Polymarket p instead
# (otherwise q≥0.85 dead-stops all fills for hours).
STRICT_REAL_FREEZE: dict[str, Any] = {
    "min_edge": 0.14,
    "min_conviction": 0.93,
    "min_conviction_guard": 0.96,
    "extreme_q_high": 0.85,
    "extreme_q_low": 0.15,
    "extreme_anchor": "q",
    "extreme_p_high": 0.72,
    "extreme_p_low": 0.28,
    "kappa_base": 0.35,
    "kappa_guard": 0.20,
    "max_single_market_pct": 0.08,
    "risk_budget": 0.18,
    "dd_guard_pct": 0.08,
    "rolling_wr_window": 20,
    "rolling_wr_floor": 0.75,
    "max_drawdown_hard_pct": 0.15,
    "n_eff_crypto": 80.0,
}

# Non-negotiable backtest gates (Hermes Agent v3)
TARGET_WR = 0.80
TARGET_WR_MEAN = 0.87
TARGET_MC_P5 = 0.82
TARGET_DD = 0.08
TARGET_PF = 2.5
TARGET_BRIER = 0.15
TARGET_SELECTIVITY_MIN = 0.04
TARGET_SELECTIVITY_MAX = 0.10

# Mode presets control entry filters + sizing. Tuned on synthetic backtests
# (seed=42, 5k markets + Monte Carlo) so that:
#   strict      → max WR, fewest trades (legacy; extreme_q band assumes inflated q)
#   strict_real → Hermes v3 production (real q; frozen — see STRICT_REAL_FREEZE)
#   moderate    → more trades, looser real-q gates (research only; fails v3 gates)
#   aggressive  → highest frequency, still aiming ~80%+ WR
#
# Keep min_edge high under real-q modes: edge <0.15 buckets destroy WR on
# VPS backtests; only the ≥0.15 bucket stayed profitable (~75% WR).
MODE_PRESETS: dict[str, dict[str, Any]] = {
    "strict": {
        "min_edge": 0.12,
        "min_conviction": 0.95,
        "min_conviction_guard": 0.97,
        "extreme_q_high": 0.88,
        "extreme_q_low": 0.12,
        "extreme_anchor": "q",
        "kappa_base": 0.35,
        "max_single_market_pct": 0.10,
        "risk_budget": 0.20,
        "n_eff_crypto": 80.0,
    },
    "strict_real": {
        # Hermes Agent v3 freeze — real cex_implied_up as q (no artificial push).
        "min_edge": STRICT_REAL_FREEZE["min_edge"],
        "min_conviction": STRICT_REAL_FREEZE["min_conviction"],
        "min_conviction_guard": STRICT_REAL_FREEZE["min_conviction_guard"],
        "extreme_q_high": STRICT_REAL_FREEZE["extreme_q_high"],
        "extreme_q_low": STRICT_REAL_FREEZE["extreme_q_low"],
        "extreme_anchor": STRICT_REAL_FREEZE["extreme_anchor"],
        "extreme_p_high": STRICT_REAL_FREEZE["extreme_p_high"],
        "extreme_p_low": STRICT_REAL_FREEZE["extreme_p_low"],
        "kappa_base": STRICT_REAL_FREEZE["kappa_base"],
        "max_single_market_pct": STRICT_REAL_FREEZE["max_single_market_pct"],
        "risk_budget": STRICT_REAL_FREEZE["risk_budget"],
        "n_eff_crypto": STRICT_REAL_FREEZE["n_eff_crypto"],
    },
    "moderate": {
        # Research / live-safer exploration only — NOT production.
        # Wider gates admit mid-odds losers under real q (VPS: ~58% WR).
        "min_edge": 0.085,
        "min_conviction": 0.88,
        "min_conviction_guard": 0.94,
        "extreme_q_high": 0.80,
        "extreme_q_low": 0.20,
        "extreme_anchor": "q",
        "kappa_base": 0.40,
        "max_single_market_pct": 0.09,
        "risk_budget": 0.20,
        "n_eff_crypto": 80.0,
    },
    "aggressive": {
        # Highest frequency of the four; WR typically ~80–83% on synthetic.
        "min_edge": 0.12,
        "min_conviction": 0.93,
        "min_conviction_guard": 0.95,
        "extreme_q_high": 0.85,
        "extreme_q_low": 0.15,
        "extreme_anchor": "q",
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


class AdvancedSignalsConfig(BaseModel):
    """Tunables for Hurst-gated multi-TF + OBI + log-normal + Kalman ensemble.

    Defaults preserve strict_real performance when history is thin (fallback to
    momentum_to_q). Never loosens entry gates — only improves q quality.
    """

    enabled: bool = True
    swarm_weight: float = Field(0.70, ge=0.0, le=1.0)
    market_blend: float = Field(0.30, ge=0.0, le=1.0)
    tf_windows: list[float] = Field(default_factory=lambda: [30.0, 60.0, 120.0, 240.0])
    tf_weights: list[float] = Field(default_factory=lambda: [0.15, 0.20, 0.30, 0.35])
    kalman_process_var: float = 1e-4
    kalman_measure_var: float = 1e-3
    hurst_window: int = 60
    garch_alpha: float = 0.08
    garch_beta: float = 0.90
    book_levels: int = 5
    # Soft conviction boost cap from sub-signal agreement (mispricing layer)
    max_conviction_boost: float = Field(0.10, ge=0.0, le=0.15)


class EnhancedMispriceConfig(BaseModel):
    """All tunable thresholds for Kelly + Bayesian conviction + risk guards.

    Hermes Agent v3 defaults match ``strict_real`` (real cex_implied_up as q).
    With calibrated model noise (Brier ≤ 0.15), filtered trades target ≥80% WR,
    MC p5 ≥ 82%, and max DD ≤ 8%.
    """

    # Profile: applies MODE_PRESETS on load (see load_enhanced_config).
    mode: FilterMode = "strict_real"

    # Advanced multi-signal ensemble (optional; zero-config default on)
    advanced: AdvancedSignalsConfig = Field(default_factory=AdvancedSignalsConfig)

    kappa_base: float = Field(0.35, ge=0.05, le=1.0)
    kappa_guard: float = Field(0.20, ge=0.05, le=1.0)
    max_single_market_pct: float = Field(0.08, ge=0.01, le=0.25)
    risk_budget: float = Field(0.18, ge=0.05, le=1.0)

    # Frozen strict_real production thresholds (see STRICT_REAL_FREEZE).
    min_edge: float = Field(0.14, ge=0.02, le=0.20)
    min_conviction: float = Field(0.93, ge=0.5, le=0.99)
    min_conviction_guard: float = Field(0.96, ge=0.5, le=0.99)
    extreme_q_high: float = Field(0.85, ge=0.55, le=0.95)
    extreme_q_low: float = Field(0.15, ge=0.05, le=0.45)
    # Synthetic / default: extreme on model q. Live real-q uses extreme_p_*.
    extreme_anchor: ExtremeAnchor = "q"
    extreme_p_high: float = Field(0.72, ge=0.55, le=0.95)
    extreme_p_low: float = Field(0.28, ge=0.05, le=0.45)
    early_exit_conviction: float = Field(0.35, ge=0.05, le=0.60)

    # Correlation-aware cap: btc5/btc15/eth5/sol5/rotator same-direction crypto
    # exposure is ONE macro risk factor (all track spot). Combined open risk
    # units on a single direction may not exceed this — below risk_budget so a
    # basket of same-way crypto bets is throttled, not stacked per-market.
    crypto_dir_risk_budget: float = Field(0.10, ge=0.02, le=0.30)

    dd_guard_pct: float = Field(0.08, ge=0.02, le=0.20)
    rolling_wr_window: int = Field(20, ge=5, le=100)
    rolling_wr_floor: float = Field(0.75, ge=0.50, le=0.95)
    max_drawdown_hard_pct: float = Field(0.15, ge=0.05, le=0.30)

    n_eff: NEffByCategory = Field(default_factory=NEffByCategory)

    bankroll: float = Field(2000.0, gt=0)
    slippage_bps_min: float = 50.0
    slippage_bps_max: float = 200.0
    # Early exit sells into the book pre-resolution: half-spread + impact paid
    early_exit_spread_bps: float = Field(150.0, ge=0.0, le=1000.0)
    # Fee on winning-side redemption (Polymarket currently 0; kept explicit)
    settlement_fee_bps: float = Field(0.0, ge=0.0, le=500.0)
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
        high = info.data.get("extreme_q_high", 0.85)
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

    @field_validator("min_edge")
    @classmethod
    def _freeze_edge_floor(cls, v: float, info) -> float:  # noqa: ANN001
        """Under strict_real, never allow min_edge below the frozen floor."""
        mode = str(info.data.get("mode") or "strict_real").strip().lower()
        floor = float(STRICT_REAL_FREEZE["min_edge"])
        if mode == "strict_real" and v + 1e-12 < floor:
            raise ValueError(
                f"strict_real forbids min_edge<{floor} (got {v}); "
                "weaker edge buckets destroy WR under real q"
            )
        return v


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
    chosen = (mode or out.get("mode") or "strict_real")
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
    # Apply remaining freeze guards when production mode
    if chosen == "strict_real":
        for key in (
            "kappa_guard",
            "dd_guard_pct",
            "rolling_wr_window",
            "rolling_wr_floor",
            "max_drawdown_hard_pct",
            "extreme_anchor",
            "extreme_p_high",
            "extreme_p_low",
        ):
            if key in STRICT_REAL_FREEZE:
                out[key] = STRICT_REAL_FREEZE[key]
    if "extreme_anchor" not in out:
        out["extreme_anchor"] = "q"
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
        If set, forces that filter profile (strict / strict_real / moderate /
        aggressive) after YAML load. Otherwise uses ``mode:`` from the YAML
        (default / production: strict_real).
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


def assert_strict_real_freeze(cfg: EnhancedMispriceConfig) -> None:
    """Raise if a strict_real config drifts from the Hermes v3 freeze."""
    if cfg.mode != "strict_real":
        return
    for key, expected in STRICT_REAL_FREEZE.items():
        if key == "n_eff_crypto":
            actual = cfg.n_eff.crypto
        else:
            actual = getattr(cfg, key)
        if isinstance(expected, str):
            if str(actual) != str(expected):
                raise AssertionError(
                    f"strict_real freeze violated: {key}={actual!r} (expected {expected!r})"
                )
        elif abs(float(actual) - float(expected)) > 1e-9:
            raise AssertionError(
                f"strict_real freeze violated: {key}={actual} (expected {expected})"
            )
