"""TV confidence tier — param modulation only (observe-only lock safe)."""

from engine.pulse.tv_confidence_tier import (
    TvTierParams,
    classify_tv_tier,
    in_sweet_spot,
    resolve_tv_entry_params,
)


def _params(**kw):
    base = TvTierParams()
    return TvTierParams(**{**base.__dict__, **kw})


def test_sweet_spot_15m_scaled_band():
    assert in_sweet_spot(
        600.0,
        window_seconds=900,
        ttc_min_base=180.0,
        ttc_max_base=240.0,
        fast_lane_15m=True,
        ttc_min_15m=160.0,
        ttc_max_15m=220.0,
    )
    assert not in_sweet_spot(
        400.0,
        window_seconds=900,
        ttc_min_base=180.0,
        ttc_max_base=240.0,
        fast_lane_15m=True,
        ttc_min_15m=160.0,
        ttc_max_15m=220.0,
    )


def test_tier_a_relaxes_on_aligned_down_mtf():
    tv = {
        "tf_confirm_mtf": "confirmed_down_mtf",
        "direction": "DOWN",
        "strength": 0.85,
        "trend_fresh_count": 3,
    }
    assert classify_tv_tier(side="down", tv_feature=tv, params=_params()) == "A"
    out = resolve_tv_entry_params(
        side="down",
        tv_feature=tv,
        ttc_s=600.0,
        window_seconds=900,
        base_min_edge=0.02,
        base_max_price=0.70,
        params=_params(enabled=True),
    )
    assert out["applied"] is True
    assert out["tier"] == "A"
    assert out["min_edge"] < 0.02
    assert out["max_price"] > 0.70


def test_tier_c_tightens_on_up_strong_opposed():
    tv = {
        "signal_level": "UP_STRONG",
        "direction": "UP",
        "strength": 0.86,
        "tf_confirm_mtf": "confirmed_up_mtf",
    }
    assert classify_tv_tier(side="down", tv_feature=tv, params=_params()) == "C"
    out = resolve_tv_entry_params(
        side="down",
        tv_feature=tv,
        ttc_s=600.0,
        window_seconds=900,
        base_min_edge=0.02,
        base_max_price=0.70,
        params=_params(enabled=True),
    )
    assert out["applied"] is True
    assert out["tier"] == "C"
    assert out["min_edge"] > 0.02
    assert out["max_price"] < 0.70


def test_outside_sweet_spot_uses_base():
    tv = {"tf_confirm_mtf": "confirmed_down_mtf", "direction": "DOWN", "strength": 0.9}
    out = resolve_tv_entry_params(
        side="down",
        tv_feature=tv,
        ttc_s=200.0,
        window_seconds=900,
        base_min_edge=0.02,
        base_max_price=0.70,
        params=_params(enabled=True, require_sweet_spot=True),
    )
    assert out["applied"] is False
    assert out["min_edge"] == 0.02
    assert out["max_price"] == 0.70


def test_disabled_leaves_base():
    out = resolve_tv_entry_params(
        side="down",
        tv_feature={"tf_confirm_mtf": "confirmed_down_mtf"},
        ttc_s=600.0,
        window_seconds=900,
        base_min_edge=0.02,
        base_max_price=0.70,
        params=_params(enabled=False),
    )
    assert out["tier"] == "base"
    assert out["applied"] is False