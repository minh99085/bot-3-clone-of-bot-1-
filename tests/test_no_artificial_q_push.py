"""No artificial q pushing — cex_implied_up is model q (lightly smoothed)."""

from __future__ import annotations

from strategy.enhanced_misprice import enhance_from_hermes_mispricing


def test_enhance_uses_cex_implied_not_extreme_push():
    opp = enhance_from_hermes_mispricing(
        market_id="m1",
        slug="btc-updown-5m-1",
        pm_implied_up=0.62,
        cex_implied_up=0.58,
        dislocation=-0.12,  # strong dislocation would have pushed q to ~0.03 before
        mp_conviction=0.8,
        timeframe="5m",
        active=True,
    )
    # Lightly smoothed: 0.5 + 0.90*(0.58-0.5) = 0.572 — NOT 0.03/0.97
    assert opp.meta["model_q"] == opp.q
    assert 0.55 <= opp.q <= 0.62
    assert opp.meta.get("model_q_source") == "cex_implied_up_smoothed"
    assert abs(opp.meta["cex_implied_up_raw"] - 0.58) < 1e-9


def test_enhance_inactive_shrinks_toward_half():
    opp = enhance_from_hermes_mispricing(
        market_id="m2",
        slug="btc-updown-5m-2",
        pm_implied_up=0.55,
        cex_implied_up=0.70,
        dislocation=0.15,
        mp_conviction=0.5,
        active=False,
    )
    # Inactive: 0.5 + 0.25*(0.70-0.5) = 0.55
    assert abs(opp.q - 0.55) < 1e-6
