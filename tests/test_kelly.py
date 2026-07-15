"""Unit tests — Polymarket Kelly Criterion (exact formulas)."""

from __future__ import annotations

import math

from strategy.kelly import apply_kappa, kelly_no, kelly_size, kelly_yes


def test_kelly_yes_formula():
    q, p = 0.80, 0.55
    assert math.isclose(kelly_yes(q, p), (q - p) / (1 - p), rel_tol=1e-9)


def test_kelly_no_formula():
    q, p = 0.20, 0.45
    assert math.isclose(kelly_no(q, p), (p - q) / p, rel_tol=1e-9)


def test_kelly_negative_edge_is_zero_after_kappa():
    assert apply_kappa(kelly_yes(0.40, 0.55), 0.35) == 0.0
    assert apply_kappa(kelly_no(0.60, 0.40), 0.35) == 0.0


def test_kelly_size_respects_10pct_cap():
    # Huge edge would want f* > 1 → kappa*1 = 0.35, but also capped at 10%
    res = kelly_size(q=0.99, p=0.20, side="YES", bankroll=2000, kappa=0.35, max_pct=0.10)
    assert res.size_usd <= 200.0 + 1e-6
    assert res.f == apply_kappa(res.f_star, 0.35)


def test_kelly_no_side_sizing():
    res = kelly_size(q=0.15, p=0.40, side="NO", bankroll=2000, kappa=0.35, max_pct=0.10)
    assert res.side == "NO"
    assert res.f_star == kelly_no(0.15, 0.40)
    assert res.size_usd > 0
