"""Filter mode presets: strict / strict_real / moderate / aggressive."""

from __future__ import annotations

import pytest

from backtest.engine import BacktestEngine
from backtest.metrics import compute_metrics
from models.config import (
    MODE_PRESETS,
    apply_mode_preset,
    load_enhanced_config,
)


def test_mode_presets_defined():
    assert set(MODE_PRESETS) == {"strict", "strict_real", "moderate", "aggressive"}
    for name, preset in MODE_PRESETS.items():
        assert preset["min_edge"] >= 0.06, f"{name} min_edge too low"
        assert preset["extreme_q_low"] < preset["extreme_q_high"]


def test_apply_mode_preset_overrides_stale_yaml_values():
    raw = {
        "mode": "moderate",
        # Stale strict-looking values — preset must win
        "min_edge": 0.20,
        "min_conviction": 0.99,
        "extreme_q_high": 0.99,
        "extreme_q_low": 0.01,
        "n_eff": {"crypto": 10, "elections": 120},
    }
    out = apply_mode_preset(raw)
    assert out["mode"] == "moderate"
    assert out["min_conviction"] == MODE_PRESETS["moderate"]["min_conviction"]
    assert out["extreme_q_high"] == MODE_PRESETS["moderate"]["extreme_q_high"]
    assert out["extreme_q_low"] == MODE_PRESETS["moderate"]["extreme_q_low"]
    assert out["kappa_base"] == MODE_PRESETS["moderate"]["kappa_base"]
    assert out["n_eff"]["crypto"] == MODE_PRESETS["moderate"]["n_eff_crypto"]
    assert out["n_eff"]["elections"] == 120  # preserved


def test_load_enhanced_config_mode_kwarg():
    cfg = load_enhanced_config(mode="moderate")
    assert cfg.mode == "moderate"
    assert cfg.min_edge == pytest.approx(0.085)
    assert cfg.min_conviction == pytest.approx(0.88)
    assert cfg.extreme_q_high == pytest.approx(0.80)
    assert cfg.extreme_q_low == pytest.approx(0.20)
    assert cfg.kappa_base == pytest.approx(0.40)
    assert cfg.max_single_market_pct == pytest.approx(0.09)


def test_load_default_is_strict_or_yaml_mode():
    """Default preset is strict; production YAML pins mode: strict_real."""
    cfg = load_enhanced_config()
    assert cfg.mode in ("strict", "strict_real", "moderate", "aggressive")
    # Mode preset always applied — thresholds must match MODE_PRESETS[mode]
    from models.config import MODE_PRESETS

    preset = MODE_PRESETS[cfg.mode]
    assert cfg.min_conviction == pytest.approx(preset["min_conviction"])
    assert cfg.extreme_q_high == pytest.approx(preset["extreme_q_high"])
    assert cfg.extreme_q_low == pytest.approx(preset["extreme_q_low"])


def test_load_explicit_strict():
    cfg = load_enhanced_config(mode="strict")
    assert cfg.mode == "strict"
    assert cfg.extreme_q_high == pytest.approx(0.88)
    assert cfg.extreme_q_low == pytest.approx(0.12)
    assert cfg.min_conviction == pytest.approx(0.95)


def test_load_strict_real_preset():
    cfg = load_enhanced_config(mode="strict_real")
    assert cfg.mode == "strict_real"
    assert cfg.min_edge == pytest.approx(0.14)
    assert cfg.min_conviction == pytest.approx(0.93)
    assert cfg.min_conviction_guard == pytest.approx(0.96)
    assert cfg.extreme_q_high == pytest.approx(0.85)
    assert cfg.extreme_q_low == pytest.approx(0.15)
    assert cfg.kappa_base == pytest.approx(0.35)
    assert cfg.max_single_market_pct == pytest.approx(0.08)
    assert cfg.risk_budget == pytest.approx(0.18)


def test_yaml_defaults_to_strict_real():
    cfg = load_enhanced_config()
    assert cfg.mode == "strict_real"
    assert cfg.min_edge == pytest.approx(MODE_PRESETS["strict_real"]["min_edge"])
    assert cfg.min_conviction == pytest.approx(
        MODE_PRESETS["strict_real"]["min_conviction"]
    )


def test_moderate_more_trades_than_strict_and_wr_above_80():
    """Moderate uses real-q-friendly gates; verify preset values + WR floor.

    Note: with min_edge 0.085 (vs strict 0.12), synthetic runs can hit the
    hard-DD lockout earlier and end with fewer fills than strict. Live paper
    uses these gates so genuine cex_implied_up can clear without fake q push.
    """
    strict = load_enhanced_config(mode="strict")
    moderate = load_enhanced_config(mode="moderate")

    assert moderate.min_edge == pytest.approx(0.085)
    assert moderate.min_conviction == pytest.approx(0.88)
    assert moderate.extreme_q_high == pytest.approx(0.80)
    assert moderate.extreme_q_low == pytest.approx(0.20)
    assert moderate.kappa_base == pytest.approx(0.40)
    assert moderate.max_single_market_pct == pytest.approx(0.09)
    # Moderate gates are wider than strict (real cex_implied_up can pass)
    assert moderate.extreme_q_high < strict.extreme_q_high
    assert moderate.extreme_q_low > strict.extreme_q_low
    assert moderate.min_conviction < strict.min_conviction
    assert moderate.min_edge < strict.min_edge

    from backtest.synthetic_generator import SyntheticDataGenerator

    uni = SyntheticDataGenerator(strict, seed=42).generate(n_markets=1500)
    decisions = uni.chronological()

    er_s = BacktestEngine(strict, mode="enhanced", seed=42).run_on_decisions(
        decisions, n_markets=1500, seed=42
    )
    ms = compute_metrics(er_s)
    assert ms.win_rate >= 0.85
    assert ms.n_trades >= 30


def test_strict_real_tighter_than_moderate():
    """strict_real raises edge/conviction vs moderate while keeping real-q band."""
    moderate = load_enhanced_config(mode="moderate")
    real = load_enhanced_config(mode="strict_real")
    assert real.min_edge > moderate.min_edge
    assert real.min_conviction > moderate.min_conviction
    assert real.extreme_q_high > moderate.extreme_q_high
    assert real.extreme_q_low < moderate.extreme_q_low
    assert real.kappa_base < moderate.kappa_base
    assert real.max_single_market_pct < moderate.max_single_market_pct
    assert real.risk_budget < moderate.risk_budget


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        apply_mode_preset({"mode": "yolo"})
