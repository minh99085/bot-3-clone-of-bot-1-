"""Tests for PRISM Phase 6 — agents, capital allocation, sizing, adversarial (PAPER ONLY)."""

from engine.pulse.prism.agents import (
    AgentConfig,
    AgentKind,
    CapitalAllocator,
    adjust_confidence_spread_widening,
    adjust_size_depth_drop,
    boost_rank_stale_book,
    classify_agent,
)


def test_classify_agent_tiers():
    cfg = AgentConfig()
    assert classify_agent(0.15, 0.80, 0.80, cfg) == AgentKind.SNIPER
    # high R but low I/C -> not sniper; R above harvester band -> NONE
    assert classify_agent(0.15, 0.40, 0.50, cfg) == AgentKind.NONE
    assert classify_agent(0.04, 0.50, 0.60, cfg) == AgentKind.HARVESTER
    assert classify_agent(0.01, 0.90, 0.90, cfg) == AgentKind.NONE
    # R in [0.06, r_min_sniper) with low I -> NONE (gap between harvester and sniper)
    assert classify_agent(0.08, 0.40, 0.50, cfg) == AgentKind.NONE


def test_sizing_scales_with_rank_and_caps():
    alloc = CapitalAllocator(bankroll_usd=50.0)
    s = alloc.size_usd(AgentKind.SNIPER, R=0.20, C=0.9, ask=0.5, depth_usd=500,
                       thompson_mult=1.0, p_win=0.65)
    assert s.size_usd > 0
    assert "half_kelly" in s.caps_applied           # kelly caps the raw

    # None agent -> zero
    z = alloc.size_usd(AgentKind.NONE, R=0.2, C=0.9, ask=0.5, depth_usd=500)
    assert z.size_usd == 0.0


def test_agent_hard_caps():
    alloc = CapitalAllocator(bankroll_usd=100000.0)   # huge bankroll -> agent cap binds
    sn = alloc.size_usd(AgentKind.SNIPER, R=1.0, C=1.0, ask=0.5, depth_usd=1e9,
                        thompson_mult=1.0, p_win=0.99)
    assert sn.size_usd <= 200.0 + 1e-9
    hv = alloc.size_usd(AgentKind.HARVESTER, R=0.05, C=1.0, ask=0.5, depth_usd=1e9,
                        thompson_mult=1.0, p_win=0.99)
    assert hv.size_usd <= 25.0 + 1e-9


def test_depth_cap_binds():
    alloc = CapitalAllocator(bankroll_usd=50.0)
    s = alloc.size_usd(AgentKind.SNIPER, R=1.0, C=1.0, ask=0.5, depth_usd=8.0,
                       thompson_mult=1.0, p_win=0.99)
    assert s.size_usd <= 0.25 * 8.0 + 1e-9          # <= 25% of depth
    assert "depth_25pct" in s.caps_applied


def test_daily_loss_halt():
    cfg = AgentConfig()
    alloc = CapitalAllocator(bankroll_usd=50.0, cfg=cfg)
    # sniper slice = 0.35*50 = 17.5; 12% halt = 2.10 loss
    alloc.record_pnl(AgentKind.SNIPER, -3.0, now=1_000_000.0)
    s = alloc.size_usd(AgentKind.SNIPER, R=0.5, C=0.9, ask=0.5, depth_usd=500, p_win=0.7)
    assert s.halted is True and s.size_usd == 0.0


def test_open_correlation_haircut():
    alloc = CapitalAllocator(bankroll_usd=50.0)
    full = alloc.size_usd(AgentKind.SNIPER, R=0.2, C=0.9, ask=0.5, depth_usd=500,
                          thompson_mult=1.0, open_corr=0.0, p_win=0.7)
    half = alloc.size_usd(AgentKind.SNIPER, R=0.2, C=0.9, ask=0.5, depth_usd=500,
                          thompson_mult=1.0, open_corr=0.5, p_win=0.7)
    assert half.size_usd < full.size_usd


def test_adversarial_adjusters():
    assert adjust_confidence_spread_widening(0.9, 0.10, 0.04) == 0.9 * 0.7   # 2.5x widening
    assert adjust_confidence_spread_widening(0.9, 0.05, 0.04) == 0.9         # no widening
    assert adjust_size_depth_drop(10.0, 40.0, 100.0) == 5.0                  # 60% drop -> halve
    assert adjust_size_depth_drop(10.0, 90.0, 100.0) == 10.0                 # small drop -> keep
    assert boost_rank_stale_book(0.10, cex_move_bps=8.0, ask_move=0.005) > 0.10   # stale book
    assert boost_rank_stale_book(0.10, cex_move_bps=1.0, ask_move=0.005) == 0.10  # cex didn't move


def test_agent_config_from_env(monkeypatch):
    monkeypatch.setenv("PULSE_PRISM_SNIPER_R_MIN", "0.20")
    monkeypatch.setenv("PULSE_PRISM_DAILY_LOSS_HALT_PCT", "0.10")
    cfg = AgentConfig.from_env()
    assert cfg.r_min_sniper == 0.20 and cfg.daily_loss_halt_pct == 0.10
