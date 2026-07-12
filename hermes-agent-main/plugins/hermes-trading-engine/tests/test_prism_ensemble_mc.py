"""Tests for PRISM Phase 4 — MC ensemble edge E and confidence C (PAPER ONLY)."""

import math

from engine.pulse.monte_carlo import HAVE_NUMPY
from engine.pulse.prism.ensemble_mc import (
    EnsembleInput,
    cex_drift_mu,
    run_ensemble,
    tv_drift_mu,
)

_SIGMA = 2.5e-5


def _base(**kw):
    d = dict(s_now=64000.0, s_open=64000.0, sigma_per_sec=_SIGMA, ttc_s=1800.0,
             ask_up=0.50, ask_down=0.50, tv_score_normalized=0.0)
    d.update(kw)
    return EnsembleInput(**d)


def test_tv_drift_mu_capped_and_signed():
    cap = 0.30 * _SIGMA / math.sqrt(3600.0)
    assert abs(tv_drift_mu(1.0, _SIGMA) - cap) < 1e-18
    assert tv_drift_mu(-1.0, _SIGMA) < 0
    assert tv_drift_mu(0.0, _SIGMA) == 0.0
    assert abs(tv_drift_mu(2.0, _SIGMA)) <= cap + 1e-18   # clamped to |1|


def test_cex_drift_mu_bounded():
    assert cex_drift_mu(0.0) == 0.0
    assert cex_drift_mu(1e6) == cex_drift_mu(50.0)        # capped at cap_bps
    assert cex_drift_mu(-1e6) == cex_drift_mu(-50.0)


def test_m1_m2_close_when_tv_zero():
    r = run_ensemble(_base(tv_score_normalized=0.0), n_paths=40000, seed=7)
    assert abs(r.models["M1_closed_form"] - r.models["M2_informed_drift"]) < 0.02


def test_high_tv_shifts_p_up_directionally():
    up = run_ensemble(_base(tv_score_normalized=1.0), n_paths=40000, seed=7)
    dn = run_ensemble(_base(tv_score_normalized=-1.0), n_paths=40000, seed=7)
    assert up.p_up_mean > 0.52
    assert dn.p_up_mean < 0.48
    assert up.models["M2_informed_drift"] > up.models["M1_closed_form"]


def test_edge_negative_when_ask_far_above_fair():
    r = run_ensemble(_base(ask_up=0.90, side="up"), n_paths=20000, seed=7)
    assert r.E < 0.0


def test_confidence_drops_when_models_disagree():
    agree = run_ensemble(_base(tv_score_normalized=0.0), n_paths=40000, seed=7)
    disagree = run_ensemble(
        _base(s_now=64010.0, tv_score_normalized=1.0, markov_state="chop_noise",
              liquidity_danger=True), n_paths=40000, seed=7)
    assert disagree.p_up_std > agree.p_up_std
    assert disagree.C < agree.C


def test_positive_edge_when_fair_above_ask():
    # cheap up ask vs a fair pushed up by strong TV -> positive E on the up side
    r = run_ensemble(_base(ask_up=0.45, ask_down=0.60, tv_score_normalized=1.0),
                     n_paths=40000, seed=7)
    assert r.side == "up"
    assert r.E > 0.0


def test_side_selection_picks_best_ev():
    r = run_ensemble(_base(ask_up=0.70, ask_down=0.40), n_paths=20000, seed=7)
    # fair ~0.5: ev_down = 0.5 - 0.40 = +0.10 > ev_up = 0.5 - 0.70 = -0.20
    assert r.side == "down"


def test_result_to_dict_shape():
    r = run_ensemble(_base(), n_paths=20000, seed=7)
    d = r.to_dict()
    for k in ("p_up_mean", "p_up_std", "E", "C", "side", "used_numpy", "models"):
        assert k in d
    assert 0.0 <= d["C"] <= 1.0


def test_numpy_flag_reflected():
    r = run_ensemble(_base(), n_paths=5000, seed=7)
    assert r.used_numpy is HAVE_NUMPY
    if not HAVE_NUMPY:
        assert r.C == 0.5


# --------------------------------------------------------------------------------------------- #
# Engine integration: observe-only ensemble populates status + a non-zero R when info is present.
# --------------------------------------------------------------------------------------------- #

def test_engine_prism_ensemble_status_and_rank(tmp_path):
    from engine.pulse.engine import PulseConfig, PulseEngine
    from engine.pulse.fair_value import RollingVol
    from engine.pulse.markets import PulseWindow
    from engine.pulse.price import PulsePriceFeed

    t0 = 3_000_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="btc-up-or-down-hourly-3000000",
                      title="Bitcoin Up or Down", open_ts=t0, close_ts=t0 + 3600,
                      up_token_id="U", down_token_id="D", window_seconds=3600)
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 3.0
        return price["p"]

    class _Mkt:
        def active_windows(self, now=None, **kw):
            return [win]
        def hydrate_books(self, w):
            # cheap DOWN ask so the down-only candidate has a positive-EV book
            from engine.pulse.markets import OrderBook
            w.up_book = OrderBook(best_bid=0.55, best_ask=0.60, ask_depth_usd=500, bid_depth_usd=500,
                                  bids=[(0.55, 900)], asks=[(0.60, 900)])
            w.down_book = OrderBook(best_bid=0.34, best_ask=0.38, ask_depth_usd=500,
                                    bid_depth_usd=500, bids=[(0.34, 900)], asks=[(0.38, 900)])
        def resolve_up(self, *a, **k):
            return None

    feed = PulsePriceFeed(fetcher=fetch, vol=RollingVol(window_s=900, min_samples=8),
                          max_open_lag_s=600.0)
    eng = PulseEngine(
        PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.01, edge_buffer=0.0,
                    basis_buffer=0.0, min_seconds_since_open=0.0, sigma_trust_floor=0.0,
                    min_vol_samples=2, directional_down_only=True,
                    prism_enabled=True,                 # Phase 4 observe-only ensemble ON
                    data_dir=str(tmp_path), fresh_start=True),
        market_feed=_Mkt(), price_feed=feed)

    for i in range(12):                                 # warm vol before open
        eng.tick(now=t0 - 12 + i)
    # seed information completeness so I>0 (chainlink anchor observed when price is fresh)
    eng.prism_info.observe("chainlink_anchor", t0 + 40, t0 + 40)
    eng.prism_info.observe("cex_lead", t0 + 40, t0 + 40)
    eng.prism_info.observe("quant_fair", t0 + 40, t0 + 40)
    eng.tick(now=t0 + 40)

    rep = eng.status()["prism_ensemble"]
    assert rep["enabled"] is True
    assert rep["used_numpy"] is HAVE_NUMPY
    # DOWN ask 0.38 vs fair ~0.5 -> positive down edge -> E>0 -> with I>0 and C>0, R>0
    assert rep["E"] > 0.0
    assert rep["I"] > 0.0
    assert rep["R"] > 0.0
