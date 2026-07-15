"""Portfolio allocation unit tests — LW / HRP / BL / cut-reduce."""

from __future__ import annotations

import numpy as np

from hermes.models import (
    ConfidenceTier,
    Direction,
    EntryMode,
    Regime,
    Signal,
    SubStrategyAction,
)
from hermes.portfolio import (
    allocate,
    black_litterman_tilt,
    edge_weighted_risk_parity,
    hrp_weights,
    ledoit_wolf_shrink,
)
from hermes.substrategy import (
    decide_action,
    default_confidence,
    infer_market_series,
    make_substrategy_id,
)


def _sig(**kw) -> Signal:
    base = dict(
        market_id="mkt_btc_100k",
        slug="btc-updown",
        question="Will BTC go up?",
        direction=Direction.NO,
        entry_mode=EntryMode.MEAN_REVERSION,
        confidence_tier=ConfidenceTier.A,
        conviction=0.8,
        fair_value=0.75,
        market_price=0.58,
        expected_edge=0.09,
        live_ev=0.075,
        regime=Regime.MEAN_REVERT,
        hourly_bucket=14,
        size_usd_suggested=150.0,
        entry_vwap_target=0.585,
        pre_entry_stability_ok=True,
        market_series="btc_updown",
        meta={"paper": True, "grok_conviction": 0.7, "tv_alignment": 0.6},
    )
    base.update(kw)
    return Signal(**base)


def test_ledoit_wolf_never_raw_and_psd():
    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.02, size=(40, 4))
    cov, shrink = ledoit_wolf_shrink(rets)
    assert 0.0 <= shrink <= 1.0
    assert cov.shape == (4, 4)
    eig = np.linalg.eigvalsh(cov)
    assert np.all(eig > 0)


def test_hrp_weights_sum_to_one():
    rng = np.random.default_rng(1)
    rets = rng.normal(0, 0.02, size=(50, 5))
    cov, _ = ledoit_wolf_shrink(rets)
    w = hrp_weights(cov)
    assert abs(w.sum() - 1.0) < 1e-8
    assert np.all(w >= 0)


def test_edge_rp_and_bl_tilt():
    vols = np.array([0.1, 0.2, 0.15])
    edges = np.array([0.08, 0.04, 0.06])
    prior = edge_weighted_risk_parity(vols, edges)
    assert abs(prior.sum() - 1.0) < 1e-8
    cov = np.diag(vols ** 2)
    # High confidence on sleeve 0
    bl = black_litterman_tilt(
        prior, cov, np.array([0.1, 0.02, 0.03]), np.array([0.9, 0.2, 0.2])
    )
    assert abs(bl.sum() - 1.0) < 1e-8
    assert bl[0] > prior[0]  # high-conf view tilts toward sleeve 0


def test_cut_model_broken_vs_reduce_losing():
    c = default_confidence("btc_updown|osmani_lane|high_vol|h15")
    c.entry_mode = EntryMode.OSMANI_LANE
    c.sample_n = 20
    c.rolling_ev = 0.01
    c.internal_confidence = 0.3
    c = decide_action(c)
    assert c.action == SubStrategyAction.CUT
    assert c.model_broken is True

    c2 = default_confidence("btc_updown|mean_reversion|mean_revert|h14")
    c2.sample_n = 12
    c2.rolling_ev = 0.07
    c2.internal_confidence = 0.35
    c2.currently_losing = True
    c2.ev_trend = -0.02
    c2 = decide_action(c2)
    assert c2.action == SubStrategyAction.REDUCE
    assert c2.model_broken is False


def test_allocate_sizes_signals():
    sigs = [
        _sig(market_id="mkt_btc_a", live_ev=0.08),
        _sig(
            market_id="mkt_eth_a",
            slug="eth-updown",
            question="Will ETH go up?",
            market_series="eth_updown",
            live_ev=0.07,
            regime=Regime.MEAN_REVERT,
        ),
    ]
    proposal, sized = allocate(sigs, capital_usd=10_000, open_exposure_usd=0, paper=True)
    assert proposal.capital_usd == 10_000
    assert proposal.diversification_ratio >= 1.0
    assert len(sized) == 2
    assert sum(s.allocation_usd for s in sized) > 0


def test_market_series_and_id():
    assert infer_market_series("x", "btc-5m-updown", "") == "btc_updown"
    assert infer_market_series("x", "eth-hourly", "ethereum") == "eth_updown"
    sid = make_substrategy_id("btc_updown", EntryMode.MOMENTUM, Regime.TRENDING_DOWN, 20)
    assert sid == "btc_updown|momentum|trending_down|h20"
