"""Tests for SAWR (Self-Adjusting Win-Rate) controller."""

from __future__ import annotations

from types import SimpleNamespace

from engine.pulse.sawr_controller import (
    SawrConfig,
    SawrController,
    wilson_lower,
)


def test_wilson_lower_bounds():
    assert wilson_lower(0, 0) == 0.0
    assert 0.0 <= wilson_lower(5, 10) <= 0.5
    assert wilson_lower(9, 10) > wilson_lower(5, 10)


def test_utility_rises_with_wr():
    c = SawrController(SawrConfig(enabled=True, min_samples=3))
    for _ in range(10):
        c.record_settled(won=True, pnl_usd=1.0, side="up", asset="btc", lane="15m")
    u_hi = c.utility()
    c2 = SawrController(SawrConfig(enabled=True, min_samples=3))
    for i in range(10):
        c2.record_settled(won=(i < 3), pnl_usd=1.0 if i < 3 else -1.0,
                          side="up", asset="btc", lane="15m")
    u_lo = c2.utility()
    assert u_hi > u_lo


def test_kill_floor_tightens_and_vetoes_loosen():
    c = SawrController(SawrConfig(
        enabled=True, min_samples=8, kill_wr=0.55, cooldown_settlements=1))
    for i in range(10):
        c.record_settled(won=(i < 3), pnl_usd=-1.0, side="up", asset="btc", lane="1h")
    eng = SimpleNamespace(
        cfg=SimpleNamespace(min_edge=0.02, min_entry_price=0.45,
                            exec_min_ev_after_slippage=0.01),
        tier_engine=None,
    )
    adj = c.maybe_adjust(eng)
    assert adj is not None
    assert adj["action"] == "tighten"
    assert c.veto_loosen() is True
    assert eng.cfg.min_edge > 0.02


def test_side_affinity_soft_blocks_losing_side():
    c = SawrController(SawrConfig(enabled=True, side_min_n=4, soft_block_edge=0.02))
    for _ in range(12):
        c.record_settled(won=False, pnl_usd=-1.0, side="up", asset="btc", lane="15m")
    ev = c.evaluate_pre_trade(side="up", ask=0.55, asset="btc", lane="15m")
    assert ev["affinity_n"] >= 4
    assert ev["soft_block"] is True
    assert ev["size_mult"] <= 0.5


def test_side_affinity_boosts_winning_side():
    c = SawrController(SawrConfig(enabled=True, side_min_n=4))
    for _ in range(12):
        c.record_settled(won=True, pnl_usd=1.0, side="down", asset="eth", lane="15m")
    ev = c.evaluate_pre_trade(side="down", ask=0.48, asset="eth", lane="15m")
    assert ev["affinity_mean"] > 0.55
    assert ev["size_mult"] >= 1.0
    assert ev["soft_block"] is False


def test_state_roundtrip():
    c = SawrController(SawrConfig(enabled=True))
    for i in range(5):
        c.record_settled(won=bool(i % 2), pnl_usd=0.5, side="up", asset="btc", lane="15m")
    st = c.to_state()
    c2 = SawrController(SawrConfig(enabled=True))
    c2.load_state(st)
    assert c2._rolling()["n"] == 5
    assert "btc|15m|up" in c2._affinity
