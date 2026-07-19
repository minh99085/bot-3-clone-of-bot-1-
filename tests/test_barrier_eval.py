"""Barrier-vs-market evaluation on real outcomes (offline, injected prices)."""

from __future__ import annotations

import math

import pytest

from backtest.barrier_eval import (
    BarrierEvalConfig,
    _implied_sigma_ann,
    evaluate_barrier,
)
from backtest.paper_ledger import RealTrade


def _trade(slug_ts, direction, entry_price, entry_cex, exit_cex, tf="5m", asset="btc"):
    return RealTrade(
        slug=f"{asset}-updown-{tf}-{slug_ts}", asset=asset, timeframe=tf,
        window_ts=slug_ts, settled_at="t", direction=direction,
        p_side=entry_price, won=True, pnl_usd=0.0, size_usd=100.0,
        entry_cex=entry_cex, exit_cex=exit_cex,
    )


def test_excludes_trades_without_cex_or_open():
    trades = [_trade(1000, "UP", 0.5, None, 64000.0)]  # no entry_cex
    rep = evaluate_barrier(trades, open_price_fn=lambda a, ts: 64000.0)
    assert rep.n_excluded == 1 and rep.n_evaluated == 0


def test_recomputes_true_outcome_from_open_not_stored_won():
    # Stored won=True, but open=64000, close(exit)=63900 → true outcome DOWN.
    t = _trade(1000, "UP", 0.5, entry_cex=64050.0, exit_cex=63900.0)
    rep = evaluate_barrier([t], open_price_fn=lambda a, ts: 64000.0)
    assert rep.n_evaluated == 1
    # market_p_up for an UP bet at 0.5 = 0.5; true_up=0 → market brier = 0.25
    assert rep.market_brier == pytest.approx(0.25, abs=1e-6)


def test_barrier_beats_market_when_spot_predicts_outcome():
    # Construct trades where fresh spot (entry_cex) already shows the eventual
    # direction but the market price is a lazy 0.5 → barrier should predict
    # outcomes better (lower Brier).
    trades = []
    for i in range(40):
        up = i % 2 == 0
        open_px = 64000.0
        # spot at entry moved in the eventual direction; close confirms it
        spot = open_px * (1.003 if up else 0.997)
        close = open_px * (1.004 if up else 0.996)
        trades.append(_trade(1000 + i, "UP" if up else "DOWN", 0.5, spot, close))
    # window path with modest vol so σ is realistic (not the saturating floor)
    def path(asset, ts0, ts1):
        base = 64000.0
        return [(ts0 + 60 * k, base * (1 + 0.001 * math.sin(k))) for k in range(6)]

    rep = evaluate_barrier(
        trades, open_price_fn=lambda a, ts: 64000.0, window_path_fn=path
    )
    assert rep.n_evaluated == 40
    assert rep.barrier_brier < rep.market_brier  # barrier is more informative
    assert rep.sigma_realized_median is not None


def test_gap_trade_sim_and_insufficient_flag():
    trades = []
    for i in range(20):
        up = i % 2 == 0
        spot = 64000.0 * (1.003 if up else 0.997)
        close = 64000.0 * (1.004 if up else 0.996)
        # market lazy at 0.5 → barrier disagrees strongly → gap trades fire
        trades.append(_trade(2000 + i, "UP", 0.5, spot, close))
    rep = evaluate_barrier(trades, open_price_fn=lambda a, ts: 64000.0)
    assert rep.n_gap_trades > 0
    assert not rep.sufficient_n  # 20 < 100
    assert "INSUFFICIENT" in rep.text().upper()
    # barrier picked the right side every time here → high WR, positive PnL
    assert rep.gap_wr > 0.9
    assert rep.gap_pnl_gross > 0


def test_implied_sigma_inversion_roundtrips():
    from strategy.advanced_signals import barrier_implied_up

    spot, strike, tau, sigma = 64128.0, 64000.0, 120.0, 0.9
    p = barrier_implied_up(spot, strike, sigma, tau)
    recovered = _implied_sigma_ann(p, spot, strike, tau)
    assert recovered is not None
    assert recovered == pytest.approx(sigma, rel=0.05)


def test_sigma_mismatch_note_fires():
    # Market prices a big move as near-coinflip (high implied σ) while realized
    # σ (from a calm path) is low → mismatch note should appear.
    trades = []
    for i in range(12):
        # market moderately confident (0.75) on a +0.25% move → implied σ ~1.9
        trades.append(_trade(3000 + i, "UP", 0.75, 64160.0, 64200.0))

    def calm_path(asset, ts0, ts1):  # very low realized vol → σ ~ floor 0.40
        return [(ts0 + 60 * k, 64000.0 * (1 + 1e-5 * k)) for k in range(6)]

    rep = evaluate_barrier(
        trades, open_price_fn=lambda a, ts: 64000.0, window_path_fn=calm_path
    )
    assert rep.sigma_realized_median is not None
    assert rep.sigma_implied_median is not None
    assert rep.sigma_implied_median > rep.sigma_realized_median
    assert any("mismatch" in n.lower() for n in rep.notes)
