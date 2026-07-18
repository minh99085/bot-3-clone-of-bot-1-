"""Task 7 — engine RNG threading + realistic early-exit fill costs.

The resolve loop used to rebuild ``np.random.default_rng(self.seed)`` on every
iteration, so the "live conviction" jitter was byte-identical for every
position. These tests pin the fix: one RNG per run, threaded through, with
jitter that actually varies across positions — while runs stay deterministic
for a given seed.
"""

from __future__ import annotations

import numpy as np
import pytest

from backtest.engine import BacktestEngine, settle_pnl
from models.config import load_enhanced_config
from models.market import DecisionPoint
from risk.portfolio_risk import PortfolioRiskManager


def _make_decisions(n: int = 8) -> list[DecisionPoint]:
    """n independent markets, one decision each, all passing the naive filter."""
    out = []
    for i in range(n):
        out.append(
            DecisionPoint(
                market_id=f"m{i:03d}",
                decision_id=f"m{i:03d}_d0",
                decision_time=float(i),
                lifetime_frac=0.5,
                category="crypto",
                days_to_resolution=0.01,
                p=0.55,
                q=0.90,
                liquidity_usd=25_000.0,
                volume_24h=60_000.0,
                true_q=0.9,
                resolved_yes=(i % 2 == 0),
                resolution_time=float(i) + 0.5,
            )
        )
    return out


def _run(seed: int = 7, monkeypatched_exit: bool = False):
    cfg = load_enhanced_config()
    eng = BacktestEngine(cfg, mode="naive", seed=seed)
    return eng.run_on_decisions(_make_decisions(), seed=seed)


def test_resolve_jitter_varies_across_positions():
    er = _run(seed=7)
    assert len(er.trades) >= 4, "harness broken: expected several settled trades"
    convs = [t.meta.get("live_conv") for t in er.trades if "live_conv" in (t.meta or {})]
    assert len(convs) >= 4, "engine must record live_conv per resolve"
    # A fresh rng per iteration produces one repeated value; a threaded rng varies.
    assert len({round(float(c), 9) for c in convs}) > 1, (
        "live-conviction jitter is identical across positions — RNG is being "
        "rebuilt inside the resolve loop"
    )


def test_same_seed_is_deterministic():
    a = _run(seed=11)
    b = _run(seed=11)
    assert [t.pnl_usd for t in a.trades] == [t.pnl_usd for t in b.trades]
    assert a.final_equity == b.final_equity


def test_early_exit_priced_with_spread_and_slippage(monkeypatch):
    """Early exits must pay spread+slippage on the way out, not a flat magic -1.5%."""
    monkeypatch.setattr(
        PortfolioRiskManager, "should_early_exit", lambda self, pos, conv: True
    )
    cfg = load_enhanced_config()
    er = BacktestEngine(cfg, mode="naive", seed=7).run_on_decisions(
        _make_decisions(), seed=7
    )
    exits = [t for t in er.trades if t.early_exit]
    assert len(exits) >= 4
    for t in exits:
        # Exit is a real fill below entry: spread + slippage cost, recorded as such
        assert t.exit_price < t.entry_price
        assert t.pnl_usd < 0
        cost_bps = (t.meta or {}).get("exit_cost_bps")
        assert cost_bps is not None and cost_bps > 0
        lo = cfg.early_exit_spread_bps + cfg.slippage_bps_min
        hi = cfg.early_exit_spread_bps + cfg.slippage_bps_max
        assert lo - 1e-6 <= cost_bps <= hi + 1e-6
    # Slippage draw must differ across positions (threaded rng again)
    assert len({round(t.exit_price / t.entry_price, 9) for t in exits}) > 1


def test_settlement_fee_reduces_winner_payout():
    from models.market import Side

    pnl0, _, _ = settle_pnl(Side.YES, 0.6, 100.0, True, fee_bps=0.0)
    pnl1, _, _ = settle_pnl(Side.YES, 0.6, 100.0, True, fee_bps=100.0)
    assert pnl1 < pnl0
    # Loser pays nothing extra (payout is zero)
    lpnl0, _, _ = settle_pnl(Side.YES, 0.6, 100.0, False, fee_bps=0.0)
    lpnl1, _, _ = settle_pnl(Side.YES, 0.6, 100.0, False, fee_bps=100.0)
    assert lpnl0 == lpnl1 == pytest.approx(-100.0)
