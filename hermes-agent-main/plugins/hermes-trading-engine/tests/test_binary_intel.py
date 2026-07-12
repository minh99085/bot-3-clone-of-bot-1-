"""Tests for invented Binary Intel suite — math, TV universal, pre/post-trade."""

from __future__ import annotations

import math

from engine.pulse.binary_intel import BinaryIntelController
from engine.pulse.binary_intel.math_core import (
    bayes_update_prob,
    binary_d2,
    binary_theta,
    compute_binary_snapshot,
    convergence_edge,
    displacement_z,
    estimation_error_kelly,
    information_gain_rsi,
    shannon_entropy_bits,
)
from engine.pulse.binary_intel.post_trade import BinaryIntelLearner
from engine.pulse.binary_intel.pre_trade import run_pre_trade_intel
from engine.pulse.binary_intel.tv_universal import cross_asset_agreement, universal_tv_snapshot


def test_shannon_entropy_max_at_half():
    assert abs(shannon_entropy_bits(0.5) - 1.0) < 1e-9
    assert shannon_entropy_bits(0.0) == 0.0
    assert shannon_entropy_bits(1.0) == 0.0
    assert shannon_entropy_bits(0.2) < 1.0


def test_displacement_and_d2():
    z = displacement_z(101.0, 100.0, 1e-4, 300.0)
    assert z is not None and z > 0
    d2 = binary_d2(101.0, 100.0, 1e-4, 300.0)
    assert d2 is not None and d2 > 0
    th = binary_theta(101.0, 100.0, 1e-4, 300.0)
    assert th is not None and math.isfinite(th)


def test_kelly_estimation_error():
    k = estimation_error_kelly(p_win=0.62, price=0.50, p_uncertainty=0.05, fraction=0.25)
    assert k["f_star"] > 0
    assert 0 < k["f_adj"] <= k["f_star"]
    k0 = estimation_error_kelly(p_win=0.40, price=0.50)
    assert k0["f_adj"] == 0.0


def test_rsi_information_gain_confirm():
    ig = information_gain_rsi(prior_p_up=0.5, lean="up", proposed_side="up", strength=0.8)
    assert ig["info_gain_bits"] > 0
    assert ig["aligned"] is True
    assert ig["posterior_p_up"] > 0.5
    fade = information_gain_rsi(prior_p_up=0.5, lean="down", proposed_side="up", strength=0.8)
    assert fade["aligned"] is False
    assert fade["posterior_p_up"] < 0.5


def test_bayes_update():
    assert bayes_update_prob(0.5, likelihood_ratio=2.0) > 0.5
    assert abs(bayes_update_prob(0.5, likelihood_ratio=1.0) - 0.5) < 1e-9


def test_convergence_edge():
    c = convergence_edge(model_p_up=0.70, poly_mid=0.55, ttc_s=200.0, window_seconds=900.0)
    assert c["abs_gap"] == round(0.15, 6)
    assert c["model_favors"] == "up"
    assert c["weighted_edge"] > 0


def test_compute_binary_snapshot_score():
    snap = compute_binary_snapshot(
        s_now=64000.0,
        s_open=63800.0,
        sigma_per_sec=1.5e-5,
        ttc_s=400.0,
        window_seconds=900.0,
        poly_mid=0.52,
        model_p_up=0.61,
        proposed_side="up",
        ask=0.54,
        rsi_lean="up",
        rsi_strength=0.75,
    )
    assert snap["intelligence_score"] >= 0.4
    assert snap["displacement_z"] is not None
    assert snap["kelly"]["f_adj"] >= 0
    assert snap["rsi_information_gain"]["aligned"] is True


def test_cross_asset_agreement():
    a = cross_asset_agreement({"lean": "up"}, {"lean": "up"})
    assert a["status"] == "agree" and a["score"] == 1.0
    c = cross_asset_agreement({"lean": "up"}, {"lean": "down"})
    assert c["status"] == "conflict"
    s = cross_asset_agreement({"lean": "up"}, None)
    assert s["status"] == "single_asset"


class _FakeIntake:
    def __init__(self, rows_by_sym):
        self._rows = rows_by_sym

    def rsi_div_history_for_symbol(self, sym):
        return list(self._rows.get(sym) or [])


def test_universal_tv_all_lanes():
    now = 1_000_000.0
    row = {
        "signal_kind": "rsi_divergence",
        "direction": "UP",
        "signal_level": "REGULAR_BULL_DIV",
        "divergence_kind": "regular_bullish",
        "strength": 0.75,
        "received_at": now - 60,
        "bar_time": now - 60,
        "rsi": 32.0,
    }
    intake = _FakeIntake({"BTCUSD": [row], "ETHUSD": [{**row, "direction": "UP"}]})

    class W:
        window_seconds = 900
        series_slug = "btc-up-or-down-15m"
        series_label = "btc_15m"

    snap = universal_tv_snapshot(intake, window=W(), now=now, proposed_side="up")
    assert snap["lane"] == "15m"
    assert snap["effective_lean"] == "up"
    assert snap["decision"]["decision"] == "confirm"
    assert snap["size_mult"] >= 1.0
    assert snap["cross_asset"]["status"] == "agree"

    class W1h:
        window_seconds = 3600
        series_slug = "eth-up-or-down-hourly"
        series_label = "eth_1h"

    snap1h = universal_tv_snapshot(intake, window=W1h(), now=now, proposed_side="up")
    assert snap1h["lane"] == "1h"
    assert snap1h["asset"] == "eth"


def test_pre_trade_intel_script():
    now = 1_000_000.0
    row = {
        "signal_kind": "rsi_divergence",
        "direction": "UP",
        "signal_level": "REGULAR_BULL_DIV",
        "divergence_kind": "regular_bullish",
        "strength": 0.8,
        "received_at": now - 30,
    }
    intake = _FakeIntake({"BTCUSD": [row]})

    class W:
        window_seconds = 900
        series_slug = "btc-up-or-down-15m"
        series_label = "btc_15m"

    out = run_pre_trade_intel(
        intake=intake,
        window=W(),
        s_now=64050.0,
        s_open=63900.0,
        sigma_per_sec=1.5e-5,
        ttc_s=350.0,
        window_seconds=900.0,
        poly_mid=0.51,
        model_p_up=0.60,
        proposed_side="up",
        ask=0.53,
        now=now,
        readiness_score=0.70,
        exploration_rate=0.0,
    )
    assert out["script"].startswith("binary_intel")
    assert out["composite_score"] > 0.5
    assert out["size_mult"] > 0
    assert out["grok_brief"]["role"] == "pre_trade_binary_intel"
    assert out["research_tags"]["tv_rsi_overlay_aligned"] is True


def test_post_trade_learner_rsi_and_lessons():
    learner = BinaryIntelLearner(enabled=True, min_samples=8, lookback_n=40)
    now = 1_000_000.0
    # Aligned wins
    for i in range(8):
        learner.record_settled(
            won=True, pnl_usd=2.0, side="up", asset="btc", lane="15m",
            intel_score=0.72, composite_score=0.74, rsi_lean="up",
            rsi_aligned=True, rsi_decision="confirm", now=now + i)
    # Opposed losses
    for i in range(6):
        learner.record_settled(
            won=False, pnl_usd=-3.0, side="up", asset="btc", lane="15m",
            intel_score=0.35, composite_score=0.30, rsi_lean="down",
            rsi_aligned=False, rsi_decision="fade", now=now + 20 + i)
    adj = learner.maybe_adjust(now=now + 100)
    assert adj is not None
    lessons = learner.lessons_for_book()
    keys = {k for _, k, _ in lessons}
    assert "binary_intel:rsi_aligned" in keys or "binary_intel:rsi_opposed" in keys
    assert learner.report()["brier"] is not None


def test_controller_roundtrip():
    ctrl = BinaryIntelController(enabled=True, grok_compute_enabled=True, exploration_rate=0.0)
    now = 1_000_000.0
    row = {
        "signal_kind": "rsi_divergence",
        "direction": "DOWN",
        "signal_level": "REGULAR_BEAR_DIV",
        "divergence_kind": "regular_bearish",
        "strength": 0.7,
        "received_at": now - 10,
    }
    intake = _FakeIntake({"ETHUSD": [row]})

    class W:
        window_seconds = 3600
        series_slug = "eth-up-or-down-hourly"
        series_label = "eth_1h"

    pre = ctrl.analyze_pre_trade(
        intake=intake,
        window=W(),
        s_now=1800.0,
        s_open=1810.0,
        sigma_per_sec=2e-5,
        ttc_s=1200.0,
        window_seconds=3600.0,
        poly_mid=0.48,
        model_p_up=0.42,
        proposed_side="down",
        ask=0.50,
        now=now,
        readiness_score=0.60,
    )
    assert pre is not None
    assert pre["tv_universal"]["lane"] == "1h"
    assert ctrl.size_mult(pre) > 0
    post = ctrl.record_settled(
        won=True, pnl_usd=1.5, side="down", asset="eth", lane="1h",
        research=pre["research_tags"] | {
            "binary_intel_score": pre["composite_score"],
            "binary_intel_intelligence": pre["intelligence_score"],
        },
        now=now + 50,
    )
    assert post is not None
    assert post["grok_autopsy"]["protocol"].startswith("binary_intel_post")
    state = ctrl.to_state()
    ctrl2 = BinaryIntelController(enabled=True)
    ctrl2.load_state(state)
    assert ctrl2.learner.report()["n"] >= 1
