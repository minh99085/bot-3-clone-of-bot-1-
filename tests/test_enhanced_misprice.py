"""Backtest must clear ≥80% WR under calibrated synthetic model."""

from __future__ import annotations

from backtest.engine import ensure_target_or_tighten, run_backtest
from models.config import load_enhanced_config
from risk.portfolio_risk import risk_unit
from strategy.enhanced_misprice import evaluate_market
from models.market import MarketSnapshot


def test_risk_unit_normalized_to_fraction():
    # $200 on $2000 bankroll at p=0.5 → f=0.1 → (0.1*0.5)^2 = 0.0025
    ru = risk_unit(200.0, 0.5, bankroll=2000.0)
    assert abs(ru - 0.0025) < 1e-9


def test_evaluate_market_hard_filter():
    m = MarketSnapshot(
        market_id="t1",
        p=0.55,
        q=0.90,
        category="crypto",
        liquidity_usd=20_000,
        volume_24h=50_000,
        seconds_to_resolution=300,
    )
    opp = evaluate_market(m)
    assert opp.passes_hard_filter
    assert opp.size_usd > 0
    assert opp.conviction >= 0.92


def test_synthetic_backtest_hits_80_wr():
    cfg = load_enhanced_config()
    result = run_backtest(config=cfg, use_synthetic=True)
    assert result.report.n_trades >= 30
    assert result.brier < 0.18
    assert result.report.win_rate >= 0.80
    assert result.report.max_drawdown_pct < 0.15
    assert result.target_met
