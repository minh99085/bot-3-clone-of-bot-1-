"""Unit tests — Bayesian Beta conviction + hard entry filter."""

from __future__ import annotations

from strategy.bayesian import (
    bayesian_conviction,
    beta_params,
    conviction_no,
    conviction_yes,
    passes_hard_entry_filter,
)


def test_beta_params_formula():
    a, b = beta_params(0.8, 60)
    assert abs(a - 0.8 * 60) < 1e-6
    assert abs(b - 0.2 * 60) < 1e-6


def test_conviction_yes_high_when_q_above_p():
    c = conviction_yes(q=0.90, p=0.55, n_eff=80)
    assert c.conviction >= 0.92


def test_conviction_no_symmetric():
    c = conviction_no(q=0.10, p=0.40, n_eff=80)
    assert c.conviction >= 0.92


def test_hard_filter_requires_extreme_q():
    ok, reasons = passes_hard_entry_filter(
        q=0.60, p=0.45, conviction=0.99, min_edge=0.06, min_conviction=0.92
    )
    assert not ok
    assert any("extreme" in r for r in reasons)


def test_hard_filter_passes_extreme_yes():
    bayes = bayesian_conviction(0.90, 0.55, 80, side="YES")
    ok, reasons = passes_hard_entry_filter(
        q=0.90,
        p=0.55,
        conviction=bayes.conviction,
        min_edge=0.06,
        min_conviction=0.92,
        extreme_q_high=0.78,
        extreme_q_low=0.22,
    )
    assert ok, reasons


def test_hard_filter_live_real_q_uses_pm_stretch():
    """Live real-q path: CEX q≈0.55, PM p stretched — must not dead-stop.

    p chosen so the NO side costs 0.26 — above the 0.25 cheap-fade block
    (side<=0.25 ran 2.8% WR live, hard-blocked by design) — and q=0.36 so the
    model is confidently down (mid_q_fade requires |q-0.5| >= 0.12 to fade)."""
    q, p = 0.36, 0.74
    bayes = bayesian_conviction(q, p, 80, side="NO")
    ok_synth, _ = passes_hard_entry_filter(
        q, p, bayes.conviction,
        min_edge=0.14, min_conviction=0.93,
        extreme_q_high=0.85, extreme_q_low=0.15, extreme_anchor="q",
    )
    assert not ok_synth
    ok_live, reasons = passes_hard_entry_filter(
        q, p, bayes.conviction,
        min_edge=0.14, min_conviction=0.93,
        extreme_q_high=0.85, extreme_q_low=0.15, extreme_anchor="q",
        live_real_q=True, extreme_p_high=0.72, extreme_p_low=0.28,
    )
    assert ok_live, reasons


def test_hard_filter_live_real_q_still_blocks_unstretched_pm():
    q, p = 0.55, 0.62  # edge 0.07 < 0.14 anyway; also p not stretched
    bayes = bayesian_conviction(q, p, 80, side="NO")
    ok, reasons = passes_hard_entry_filter(
        q, p, bayes.conviction,
        min_edge=0.14, min_conviction=0.93,
        extreme_q_high=0.85, extreme_q_low=0.15,
        live_real_q=True, extreme_p_high=0.72, extreme_p_low=0.28,
    )
    assert not ok
    assert any("edge" in r or "stretched" in r or "conviction" in r for r in reasons)
