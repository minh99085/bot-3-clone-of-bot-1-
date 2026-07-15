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
