"""Filter mode presets: strict / moderate / aggressive."""

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
    assert set(MODE_PRESETS) == {"strict", "moderate", "aggressive"}
    for name, preset in MODE_PRESETS.items():
        assert preset["min_edge"] == 0.12, f"{name} must keep min_edge=0.12"
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
    assert cfg.min_conviction == pytest.approx(0.90)
    assert cfg.extreme_q_high == pytest.approx(0.85)
    assert cfg.extreme_q_low == pytest.approx(0.15)
    assert cfg.kappa_base == pytest.approx(0.33)
    assert cfg.max_single_market_pct == pytest.approx(0.09)


def test_load_default_is_strict_or_yaml_mode():
    """Default preset is strict; production YAML may pin mode: moderate."""
    cfg = load_enhanced_config()
    assert cfg.mode in ("strict", "moderate", "aggressive")
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


def test_moderate_more_trades_than_strict_and_wr_above_80():
    """Moderate must increase fill count vs strict while keeping WR ≥ 80%."""
    strict = load_enhanced_config(mode="strict")
    moderate = load_enhanced_config(mode="moderate")

    # Shared universe so the comparison is filter/sizing only
    from backtest.synthetic_generator import SyntheticDataGenerator

    uni = SyntheticDataGenerator(strict, seed=42).generate(n_markets=1500)
    decisions = uni.chronological()

    er_s = BacktestEngine(strict, mode="enhanced", seed=42).run_on_decisions(
        decisions, n_markets=1500, seed=42
    )
    er_m = BacktestEngine(moderate, mode="enhanced", seed=42).run_on_decisions(
        decisions, n_markets=1500, seed=42
    )
    ms = compute_metrics(er_s)
    mm = compute_metrics(er_m)

    assert mm.n_trades > ms.n_trades, (
        f"moderate should trade more (got {mm.n_trades} vs strict {ms.n_trades})"
    )
    assert mm.win_rate >= 0.80, f"moderate WR {mm.win_rate:.1%} below 80% floor"
    assert mm.max_drawdown_pct <= 0.15
    assert ms.win_rate >= 0.85
    assert moderate.max_single_market_pct == pytest.approx(0.09)
    assert moderate.kappa_base == pytest.approx(0.33)

def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        apply_mode_preset({"mode": "yolo"})
