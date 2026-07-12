"""WS3: promotion-gated Kelly sizing — flat until bucket promoted."""

from __future__ import annotations

from engine.pulse.sizing import sizing_diagnostics_promoted


def _promoted(dim, val):
    return dim == "direction" and val == "down_strong"


def test_flat_size_until_bucket_promoted():
    tags = {"direction": "up", "ttc_band": "180-240s"}
    d = sizing_diagnostics_promoted(
        sel_tags=tags, is_promoted=_promoted,
        p_win=0.75, price=0.5, ev_after_costs=0.1,
        bankroll_usd=1000.0, hard_cap_usd=20.0,
        daily_loss_cap_usd=50.0, daily_loss_so_far=0.0,
        base_size_usd=5.0, global_sizing_enabled=True,
    )
    assert d["promotion_gated"] is True
    assert d["bucket_promoted"] is False
    assert d["actual_size_usd"] == 5.0
    assert d["suggested_size_usd"] > 0


def test_kelly_on_when_promoted():
    tags = {"direction": "down_strong", "ttc_band": "180-240s"}
    d = sizing_diagnostics_promoted(
        sel_tags=tags, is_promoted=_promoted,
        p_win=0.75, price=0.5, ev_after_costs=0.1,
        bankroll_usd=1000.0, hard_cap_usd=20.0,
        daily_loss_cap_usd=50.0, daily_loss_so_far=0.0,
        base_size_usd=5.0, global_sizing_enabled=True,
    )
    assert d["bucket_promoted"] is True
    assert d["actual_size_usd"] > 5.0
    assert d["no_martingale"] is True


def test_loss_daily_cap_forces_zero_suggestion():
    d = sizing_diagnostics_promoted(
        sel_tags={"direction": "down_strong"},
        is_promoted=_promoted,
        p_win=0.9, price=0.5, ev_after_costs=0.2,
        bankroll_usd=1000.0, hard_cap_usd=20.0,
        daily_loss_cap_usd=50.0, daily_loss_so_far=55.0,
        base_size_usd=5.0, global_sizing_enabled=True,
    )
    assert d["daily_cap_hit"] is True
    assert d["suggested_size_usd"] == 0.0