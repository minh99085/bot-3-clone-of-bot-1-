"""Cycle-2 kill-phantom-edge: the learning blend only activates when the edge model's out-of-sample
Brier actually BEATS the market price (a calibrated model is not necessarily more accurate than the
market). Also: readiness uses a true ECE, not the Brier score, for its calibration gate. PAPER ONLY.
"""

from __future__ import annotations

from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


class _Mkt:
    def __init__(self, w):
        self._w = w

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=50000, bid_depth_usd=50000,
                              asks=[(0.55, 100000.0)], bids=[(0.50, 100000.0)])
        w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=49000, bid_depth_usd=44000,
                                asks=[(0.49, 100000.0)], bids=[(0.44, 100000.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _engine(tmp_path, **over):
    t0 = 9_960_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}
    feed = PulsePriceFeed(fetcher=lambda: price["p"], source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, data_dir=str(tmp_path), **over)
    return PulseEngine(cfg, market_feed=_Mkt(win), price_feed=feed), t0


class _StubModel:
    def __init__(self, n_labeled=1000, ece=0.05):
        self.n_labeled = n_labeled
        self._ece = ece

    def calibration_error(self):
        return self._ece


def test_market_benchmark_aggregation_and_grading(tmp_path):
    eng, t0 = _engine(tmp_path)
    # schedule then grade two windows: model close (small SE), market far (large SE) -> model beats
    eng._schedule_market_benchmark("w1", 64000.0, t0 + 300, model_p_up=0.9, market_p_up=0.5,
                                   fair_p_up=0.7)
    eng.price.poll(now=t0 + 301)                      # price 64000 == open -> outcome up (close>=open)
    eng._grade_market_benchmark(now=t0 + 301)
    b = eng._market_benchmark()
    assert b["n"] == 1 and b["model_brier"] < b["market_brier"] and b["model_beats_market"] is True


def test_learning_blend_gated_when_model_does_not_beat_market(tmp_path):
    eng, _ = _engine(tmp_path, learning_enabled=True, learning_min_samples=10,
                     learning_bench_min_samples=20, learning_max_calib_error=0.5)
    eng.edge_model = _StubModel(n_labeled=1000, ece=0.05)   # plenty of samples, well-calibrated
    # benchmark: model WORSE than market (higher squared error) over enough windows
    for _ in range(40):
        eng._mkt_bench_recent.append((0.30, 0.18, 0.25))    # model_se > market_se
    w, why = eng._learning_weight()
    assert w == 0.0 and why == "model_not_beating_market"   # phantom edge killed
    # now make the model BEAT the market -> blend re-activates
    eng._mkt_bench_recent.clear()
    for _ in range(40):
        eng._mkt_bench_recent.append((0.16, 0.22, 0.20))    # model_se < market_se
    w2, why2 = eng._learning_weight()
    assert w2 > 0.0 and why2 == "active"


def test_learning_report_includes_market_benchmark(tmp_path):
    eng, _ = _engine(tmp_path, learning_enabled=True)
    for _ in range(5):
        eng._mkt_bench_recent.append((0.2, 0.21, 0.22))
    rep = eng._learning_report()
    assert "market_benchmark" in rep and rep["market_benchmark"]["n"] == 5


def test_readiness_calibration_gate_uses_ece_not_brier(tmp_path):
    eng, _ = _engine(tmp_path)
    eng.edge_model = _StubModel(n_labeled=1000, ece=0.04)   # good ECE
    r = eng.readiness()
    # the calibration gate now reflects the model ECE (0.04 <= 0.10), not the ~0.23 Brier
    assert r["gates"]["calibration_error_ok"] is True
    assert r["metrics"]["calibration_error"] == 0.04
