"""OBSERVE-ONLY EP Chan research features — classification, safety, and no-trade-impact."""

from __future__ import annotations

import random

from engine.pulse.research_features import (hurst_exponent, classify_hurst, half_life_adf,
                                             zscore, zscore_bucket, kalman_fair_prob,
                                             autocorrelation, realized_volatility,
                                             ResearchObservatory)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


def _ar1(phi, n, seed, sigma=1.0):
    random.seed(seed)
    r = [0.0]
    for _ in range(n):
        r.append(phi * r[-1] + random.gauss(0, sigma))
    return r[1:]


# 1) Hurst regime classification --------------------------------------------------------- #
def test_hurst_regime_classification():
    h_trend = hurst_exponent(_ar1(0.85, 500, 1))      # persistent -> trending
    h_revert = hurst_exponent(_ar1(-0.85, 500, 2))    # anti-persistent -> mean reverting
    h_noise = hurst_exponent(_ar1(0.0, 500, 3))       # iid -> ~0.5 noise
    assert h_trend is not None and h_revert is not None and h_noise is not None
    assert h_trend > h_noise > h_revert               # ordering holds
    assert classify_hurst(h_trend) == "trending"
    assert classify_hurst(h_revert) == "mean_reverting"
    # small sample / NaN -> insufficient_data (never raises)
    assert classify_hurst(hurst_exponent([0.1, -0.1, 0.2])) == "insufficient_data"
    assert hurst_exponent([]) is None


# 2) half-life / ADF safety -------------------------------------------------------------- #
def test_half_life_and_adf_safety():
    # mean-reverting spread (b=0.5) -> finite positive half-life ~1 sample
    random.seed(4)
    s = [0.0]
    for _ in range(300):
        s.append(0.5 * s[-1] + random.gauss(0, 0.5))
    hl, adf, reason = half_life_adf(s[1:])
    # strong mean reversion: short half-life + significantly-negative ADF t-stat
    assert reason == "ok" and hl is not None and hl < 5 and adf is not None and adf < -3
    # random walk -> long half-life + ADF t NOT significant (distinguished by the t-stat,
    # since a finite-sample RW shows a large/biased half-life rather than exactly None)
    random.seed(5)
    rw = [0.0]
    for _ in range(300):
        rw.append(rw[-1] + random.gauss(0, 0.5))
    hl2, adf2, reason2 = half_life_adf(rw[1:])
    assert (hl2 is None) or (hl2 > 10)
    assert adf2 is None or adf2 > -2.86           # not stationary at the ~5% DF critical value
    # too few samples -> explicit diagnostic, no crash
    assert half_life_adf([0.1, 0.2, 0.3]) == (None, None, "insufficient_samples")


# 3) z-score safety + buckets ------------------------------------------------------------ #
def test_zscore_safety_and_buckets():
    assert zscore(1.0, [0.0] * 5, min_n=20) is None        # too few samples
    assert zscore(1.0, [0.5] * 30) is None                 # zero variance -> None
    _rng = random.Random(7)
    buf = [_rng.gauss(0, 1) for _ in range(50)]
    z = zscore(3.0, buf)
    assert z is not None and z > 1.5                        # 3 std-ish above a ~N(0,1) buffer
    assert zscore(None, buf) is None and zscore(float("nan"), buf) is None
    assert zscore_bucket(-3) == "<=-2" and zscore_bucket(0) == "-1..1" and zscore_bucket(2.5) == ">=2"
    assert zscore_bucket(None) == "na"


# 4) Kalman fair prob -------------------------------------------------------------------- #
def test_kalman_fair_prob_safety():
    assert kalman_fair_prob([0.5, 0.5]) == (None, "insufficient_samples")
    kf, reason = kalman_fair_prob([0.4, 0.45, 0.5, 0.55, 0.52, 0.51, 0.5, 0.53, 0.49])
    assert reason == "ok" and 0.0 <= kf <= 1.0


# 4b) autocorrelation + realized volatility (Phase 3) ----------------------------------- #
def test_autocorrelation_and_realized_vol_safety():
    assert autocorrelation([1.0, 2.0]) is None          # too few samples
    assert autocorrelation([0.5] * 20) is None           # zero variance -> None
    pos = autocorrelation(_ar1(0.8, 200, 11), lag=1)     # persistent -> positive lag-1 autocorr
    neg = autocorrelation(_ar1(-0.8, 200, 12), lag=1)    # anti-persistent -> negative
    assert pos is not None and pos > 0.3 and neg is not None and neg < -0.3
    assert -1.0 <= pos <= 1.0 and -1.0 <= neg <= 1.0
    assert realized_volatility([0.1] * 5) is None        # too few
    rv = realized_volatility(_ar1(0.0, 100, 13))
    assert rv is not None and rv > 0


# 5) missing-data handling in the observatory -------------------------------------------- #
def test_observatory_safe_with_small_samples_and_nans():
    obs = ResearchObservatory()
    obs.observe_oracle(None)
    obs.observe_oracle(float("nan"))
    obs.observe_divergence(None, None)
    f = obs.evaluate(current_divergence=None)              # empty buffers -> all safe Nones
    assert f.observe_only is True
    assert f.hurst is None and f.hurst_regime == "insufficient_data"
    assert f.half_life_s is None and f.zscore is None and f.kalman_fair_prob is None
    assert obs.coverage["candidates"] == 1
    assert obs.coverage["missing_reasons"]                 # reasons recorded


def test_observatory_evaluate_and_grouped_report():
    obs = ResearchObservatory(returns_min=32, div_min=20)
    for i in range(120):                                   # warm buffers
        obs.observe_oracle(64000.0 * (1 + 0.0003 * random.Random(i).gauss(0, 1)))
        obs.observe_divergence(0.02 * random.Random(i + 1).gauss(0, 1), 0.5)
    f = obs.evaluate(current_divergence=0.05)
    assert f.observe_only is True and f.hurst is not None and f.zscore is not None
    assert f.autocorr_lag1 is not None and f.realized_vol is not None     # Phase 3 features
    fd = f.to_dict()
    assert "autocorr_lag1" in fd and "realized_vol" in fd
    # grouped PnL/calibration by regime + z bucket
    obs.record_settled(regime="trending", zbucket=">=2", pnl=7.5, won=True,
                       fair_at_entry=0.7, outcome_up=True)
    obs.record_settled(regime="trending", zbucket=">=2", pnl=-5.0, won=False,
                       fair_at_entry=0.6, outcome_up=False)
    r = obs.report()
    assert r["observe_only"] is True and r["affects_trading"] is False
    assert r["pnl_by_regime"]["trending"]["n"] == 2
    assert r["pnl_by_regime"]["trending"]["win_rate"] == 0.5
    assert abs(r["pnl_by_regime"]["trending"]["pnl_usd"] - 2.5) < 1e-9
    assert r["pnl_by_zscore_bucket"][">=2"]["n"] == 2
    assert "coverage" in r and "missing_data_reasons" in r


# 6) OBSERVE-ONLY: features never change the trade decision ------------------------------ #
class _Mkt:
    def __init__(self, w):
        self._w = w
    def active_windows(self, now=None, **kw):
        return [self._w]
    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=550, bid_depth_usd=500,
                              asks=[(0.55, 1000.0)], bids=[(0.50, 1000.0)])
        w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=490, bid_depth_usd=440,
                                asks=[(0.49, 1000.0)], bids=[(0.44, 1000.0)])
        return w
    def fetch_resolution(self, market_id):
        return True


def _run_cycle(tmp_path, research_enabled):
    t0 = 8_500_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    eng = PulseEngine(PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False,
                                  basis_buffer=0.0, min_seconds_since_open=0.0,
                                  sigma_trust_floor=0.0, min_vol_samples=2, settle_grace_s=0.0,
                                  exec_max_depth_consume_frac=0.9,
                                  research_features_enabled=research_enabled,
                                  data_dir=str(tmp_path)),
                      market_feed=_Mkt(win), price_feed=feed)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(8):
        eng.tick(now=t0 + 2 + k * 5)
    return eng


def test_research_features_are_observe_only(tmp_path):
    on = _run_cycle(tmp_path / "on", True)
    off = _run_cycle(tmp_path / "off", False)
    # identical trading behavior with features on vs off -> features cannot affect decisions
    assert on.ledger.trades == off.ledger.trades == 1
    pos = on.ledger.positions["e1"]
    assert pos.research is not None and "hurst_regime" in pos.research   # entry-time tags logged
    st = on.status()["research_features"]
    assert st["observe_only"] is True and st["affects_trading"] is False
    assert "pnl_by_regime" in st and "coverage" in st and "missing_data_reasons" in st
    assert off.status()["research_features"] == {"enabled": False}
