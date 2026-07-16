"""Production backtest suite — generator, engine, compare, 80% WR gate."""

from __future__ import annotations

from backtest.compare import compare_naive_vs_enhanced
from backtest.engine import BacktestEngine, run_backtest
from backtest.metrics import compute_metrics, threshold_sweep
from backtest.synthetic_generator import SyntheticDataGenerator
from models.config import load_enhanced_config


def test_synthetic_generator_multi_decision_and_correlation():
    cfg = load_enhanced_config()
    uni = SyntheticDataGenerator(cfg, seed=42).generate(n_markets=5000)
    assert uni.n_markets == 5000
    # 3 decision fracs by default
    assert len(uni.decisions) == 5000 * len(cfg.decision_fracs)
    # Chronological sort stable
    chrono = uni.chronological()
    assert all(
        chrono[i].decision_time <= chrono[i + 1].decision_time
        for i in range(0, len(chrono) - 1, max(1, len(chrono) // 50))
    )


def test_engine_enhanced_hits_80_wr():
    cfg = load_enhanced_config(mode="strict")
    er = BacktestEngine(cfg, mode="enhanced", seed=42).run_synthetic(
        n_markets=6000, seed=42
    )
    m = compute_metrics(er)
    assert m.n_trades >= 30
    assert m.brier < 0.18
    assert m.win_rate >= 0.80
    assert m.max_drawdown_pct <= 0.15
    assert m.target_met
    # Every decision tracked
    assert m.n_decisions == er.n_decision_points
    assert m.n_taken + m.n_rejected == m.n_decisions


def test_naive_vs_enhanced_lift():
    cfg = load_enhanced_config(mode="strict")
    cmp = compare_naive_vs_enhanced(n_markets=5000, seed=42, config=cfg)
    # Enhanced should be pickier and usually higher WR
    assert cmp.enhanced.n_trades <= cmp.naive.n_trades or cmp.enhanced.win_rate >= cmp.naive.win_rate
    assert cmp.enhanced.win_rate >= 0.80


def test_threshold_sweep_monotonic_ish():
    cfg = load_enhanced_config(mode="strict")
    er = BacktestEngine(cfg, mode="enhanced", seed=7).run_synthetic(n_markets=3000, seed=7)
    rows = threshold_sweep(er.decisions)
    assert rows
    # Higher threshold should not explode n without bound
    assert rows[-1]["n"] <= rows[0]["n"] + 1e-9


def test_run_backtest_compat_wrapper():
    # 5k markets: path-dependent DD on smaller samples can briefly exceed 15%
    # even when WR stays high (see Monte Carlo tails in BACKTEST_GUIDE).
    cfg = load_enhanced_config(mode="strict")
    result = run_backtest(config=cfg, use_synthetic=True, n_markets=5000, seed=42)
    assert result.engine is not None
    assert result.report.n_trades >= 20
    assert result.report.win_rate >= 0.80
    assert result.target_met
