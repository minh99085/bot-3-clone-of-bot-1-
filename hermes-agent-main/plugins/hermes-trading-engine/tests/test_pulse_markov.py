"""Markov regime machine (Phase 6) — observe-only, sparse-sample safe."""

from __future__ import annotations

from engine.pulse.markov import MarkovRegime, classify_state, STATES


def test_classify_state_priority_and_mapping():
    # danger states take priority
    assert classify_state(hurst_regime="trending", signal_direction="up", stale_factor=0.9,
                          settlement_boundary_risk=0.8, spread=0.01, ask_depth_usd=500.0
                          ) == "resolution_danger"
    assert classify_state(hurst_regime="trending", signal_direction="up", stale_factor=0.0,
                          settlement_boundary_risk=0.0, spread=0.01, ask_depth_usd=5.0,
                          min_depth_usd=50.0) == "liquidity_danger"
    assert classify_state(hurst_regime="trending", signal_direction="up", stale_factor=0.0,
                          settlement_boundary_risk=0.0, spread=0.01, ask_depth_usd=500.0
                          ) == "trend_up"
    assert classify_state(hurst_regime="mean_reverting", signal_direction="down", stale_factor=0.0,
                          settlement_boundary_risk=0.0, spread=0.01, ask_depth_usd=500.0
                          ) == "mean_reverting_down"
    assert classify_state(hurst_regime="trending", signal_direction="up", stale_factor=0.7,
                          settlement_boundary_risk=0.0, spread=0.01, ask_depth_usd=500.0
                          ) == "stale_polymarket_up"
    assert classify_state(hurst_regime="noise", signal_direction="neutral", stale_factor=0.0,
                          settlement_boundary_risk=0.0, spread=0.01, ask_depth_usd=500.0
                          ) == "chop_noise"


def test_markov_sparse_samples_safe():
    m = MarkovRegime(min_samples=20)
    for _ in range(3):
        m.observe("trend_up")
        m.record_terminal(state="trend_up", accepted=True)
    out = m.state_outputs("trend_up")
    assert out["reason"] == "insufficient_samples"
    assert out["p_resolve_up"] is None and out["p_edge_survives_execution"] is None


def test_markov_outputs_when_enough_samples():
    m = MarkovRegime(min_samples=10)
    for i in range(20):
        m.observe("trend_up")
        m.record_terminal(state="trend_up", accepted=(i % 2 == 0))    # 50% edge survives
    for i in range(15):
        m.record_resolution(state="trend_up", outcome_up=(i % 5 != 0))  # 80% up
    out = m.state_outputs("trend_up")
    assert out["reason"] == "ok"
    assert out["p_edge_survives_execution"] == 0.5
    assert out["p_no_trade_best"] == 0.5
    assert abs(out["p_resolve_up"] - 0.8) < 1e-9 and abs(out["p_resolve_down"] - 0.2) < 1e-9


def test_markov_transitions_and_report():
    m = MarkovRegime()
    seq = ["trend_up", "trend_up", "chop_noise", "trend_up"]
    for s in seq:
        m.observe(s)
    tp = m.transition_probs()
    assert "trend_up" in tp and abs(sum(tp["trend_up"].values()) - 1.0) < 1e-9
    r = m.report()
    assert r["observe_only"] is True and r["affects_trading"] is False
    assert r["state_coverage"]["trend_up"] == 3 and set(STATES) <= set(r["state_coverage"])
