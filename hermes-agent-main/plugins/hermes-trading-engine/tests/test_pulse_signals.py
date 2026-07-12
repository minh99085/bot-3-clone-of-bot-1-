"""Simons-style raw signal engine (Phase 4) — observe-only, multi-horizon, safe."""

from __future__ import annotations

from engine.pulse.signals import SignalEngine, SignalSnapshot


def _feed(se, prices, t0=1000.0, dt=1.0):
    for i, p in enumerate(prices):
        se.observe_price(p, now=t0 + i * dt)
    return t0 + (len(prices) - 1) * dt


def test_signal_snapshot_safe_with_no_data():
    se = SignalEngine()
    s = se.snapshot(ttc_s=120.0, now=1000.0)
    assert s.observe_only is True and s.family == "insufficient_data"
    assert s.direction == "neutral" and s.strength == 0.0


def test_signal_detects_uptrend_direction_and_family():
    se = SignalEngine(horizons=(15, 30, 60, 180), window_s=300)
    now = _feed(se, [64000.0 + i * 3 for i in range(200)])   # steady uptrend, 1s cadence
    s = se.snapshot(ttc_s=120.0, now=now)
    assert s.direction == "up" and s.strength > 0.0
    assert s.returns["15s"] is not None and s.returns["180s"] is not None
    assert s.family in ("momentum", "microstructure", "noise", "mean_reversion")
    assert 0.0 <= s.confidence <= 1.0


def test_signal_engine_coverage_and_observe_only():
    se = SignalEngine()
    now = _feed(se, [64000.0 + (i % 5) for i in range(120)])
    se.observe_poly(0.5, 0.02, 500.0, now=now - 60)
    se.observe_poly(0.55, 0.01, 800.0, now=now)
    se.snapshot(ttc_s=100.0, now=now)
    r = se.report()
    assert r["observe_only"] is True and r["affects_trading"] is False
    assert r["snapshots"] == 1 and r["tv_direction_available"] is False
    assert "by_family" in r and "by_direction" in r


def test_signal_tv_direction_none_when_unavailable():
    se = SignalEngine()
    s = se.snapshot(ttc_s=50.0, now=1000.0)
    assert s.tv_direction is None        # no TradingView feed -> explicitly None
