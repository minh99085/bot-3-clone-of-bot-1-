"""Task 5 (net costs into edge before Kelly + min_edge) and Task 6
(correlation-aware crypto risk cap)."""

from __future__ import annotations

import pytest

from models.config import load_enhanced_config
from models.market import MarketSnapshot, OpenPosition, Side, TradeOpportunity
from risk.portfolio_risk import PortfolioRiskManager
from strategy.enhanced_misprice import evaluate_market
from strategy.kelly import net_edge_after_costs, expected_cost_frac


# --- Task 5: costs netted before Kelly & min_edge ---------------------------

def test_net_edge_helper_subtracts_slippage_and_fee():
    # YES side: p_side=0.40, q_side=0.60 → gross 0.20
    gross = 0.60 - 0.40
    cost = expected_cost_frac(0.40, 0.60, slippage_bps=125, fee_bps=100)
    net = net_edge_after_costs(0.40, 0.60, slippage_bps=125, fee_bps=100)
    assert cost > 0
    assert net == pytest.approx(gross - cost)
    assert net < gross


def test_positive_gross_but_net_negative_sizes_to_zero():
    """A nominal-positive edge that costs cannot overcome must size to zero."""
    cfg = load_enhanced_config(mode="moderate")
    # Force costs huge so any real edge nets negative
    cfg.slippage_bps_min = 3000.0
    cfg.slippage_bps_max = 3000.0
    m = MarketSnapshot(
        market_id="c1", slug="btc-updown-5m-1", category="crypto",
        p=0.55, q=0.68,  # gross edge 0.13 > moderate min_edge
        liquidity_usd=20_000, volume_24h=50_000, seconds_to_resolution=300,
    )
    opp = evaluate_market(m, config=cfg, live_real_q=True)
    assert opp.size_usd == 0.0
    assert not opp.passes_hard_filter
    assert any("net" in r.lower() for r in opp.reasons)


def test_costs_shrink_kelly_size_vs_gross():
    """With costs on, the sized position is strictly smaller than gross-edge Kelly."""
    cfg = load_enhanced_config(mode="moderate")
    # Stretched market clearing the live_real_q fade gates: NO side priced
    # 0.26 (above the 0.25 cheap-fade block) and q=0.36 confidently down
    # (mid_q_fade requires |q-0.5| >= 0.12 to fade a stretched p).
    m = MarketSnapshot(
        market_id="c2", slug="btc-updown-5m-2", category="crypto",
        p=0.74, q=0.36, liquidity_usd=50_000, volume_24h=80_000,
        seconds_to_resolution=300,
    )
    cfg_free = cfg.model_copy(deep=True)
    cfg_free.slippage_bps_min = 0.0
    cfg_free.slippage_bps_max = 0.0
    cfg_free.settlement_fee_bps = 0.0
    cfg_free.max_single_market_pct = 0.95  # lift cap so size tracks Kelly f
    cfg_cost = cfg.model_copy(deep=True)
    cfg_cost.slippage_bps_min = 200.0
    cfg_cost.slippage_bps_max = 200.0
    cfg_cost.settlement_fee_bps = 100.0
    cfg_cost.max_single_market_pct = 0.95
    free = evaluate_market(m, config=cfg_free, live_real_q=True)
    cost = evaluate_market(m, config=cfg_cost, live_real_q=True)
    assert free.size_usd > 0
    assert cost.size_usd < free.size_usd
    assert cost.meta.get("net_edge") < cost.meta.get("gross_edge")


# --- Task 6: correlation-aware crypto directional cap -----------------------

def _crypto_opp(mid: str, side: Side, risk_unit: float = 0.06) -> TradeOpportunity:
    return TradeOpportunity(
        market_id=mid, slug=f"{mid}-updown-5m-1", side=side,
        p=0.5, q=0.8, edge=0.3, conviction=0.98, conviction_score=0.9,
        kelly_f_star=0.3, kelly_f=0.1, kappa=0.35, size_usd=120.0,
        risk_unit=risk_unit, liquidity_score=1.0, time_decay_factor=1.0,
        passes_hard_filter=True, meta={"category": "crypto"},
    )


def test_four_same_direction_crypto_bets_throttled():
    cfg = load_enhanced_config(mode="strict_real")
    rm = PortfolioRiskManager(cfg)
    rm.state.bankroll = 10_000.0
    # 4 correlated same-direction (UP) crypto bets, each within per-market caps
    opps = [
        _crypto_opp("btc5", Side.UP),
        _crypto_opp("btc15", Side.UP),
        _crypto_opp("eth5", Side.UP),
        _crypto_opp("sol5", Side.UP),
    ]
    chosen = rm.select_within_budget(opps)
    # Combined same-direction crypto exposure is one risk factor → not all 4 fit
    assert len(chosen) < 4, "same-direction crypto bets must be throttled as one factor"
    total_dir_ru = sum(o.risk_unit for o in chosen)
    assert total_dir_ru <= cfg.crypto_dir_risk_budget + 1e-9


def test_opposite_direction_crypto_not_throttled_together():
    """UP and DOWN crypto bets are not the same risk factor — both can fit."""
    cfg = load_enhanced_config(mode="strict_real")
    rm = PortfolioRiskManager(cfg)
    rm.state.bankroll = 10_000.0
    up = _crypto_opp("btc5", Side.UP, risk_unit=0.05)
    down = _crypto_opp("eth5", Side.DOWN, risk_unit=0.05)
    chosen = rm.select_within_budget([up, down])
    assert len(chosen) == 2


def test_directional_cap_blocks_add_when_exposed():
    cfg = load_enhanced_config(mode="strict_real")
    rm = PortfolioRiskManager(cfg)
    rm.state.bankroll = 10_000.0
    # Pre-load open UP crypto exposure near the directional cap
    rm.state.open_positions.append(
        OpenPosition(
            position_id="p1", market_id="btc5", slug="btc-updown-5m-1",
            side=Side.UP, entry_price=0.5, size_usd=120.0, shares=240.0,
            q_at_entry=0.8, conviction_at_entry=0.98,
            risk_unit=cfg.crypto_dir_risk_budget - 0.005,
            meta={"category": "crypto"},
        )
    )
    another_up = _crypto_opp("eth5", Side.UP, risk_unit=0.02)
    ok, reason = rm.can_add(another_up)
    assert not ok
    assert "crypto" in reason.lower() or "direction" in reason.lower()
