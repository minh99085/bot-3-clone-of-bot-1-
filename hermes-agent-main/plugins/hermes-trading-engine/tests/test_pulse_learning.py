"""Closed-loop learning: the bot's calibrated edge model adjusts the directional decision and
grows as it runs — WITHOUT bypassing the execution gate, paper-realism, or reconciliation.

Covers: edge-model decision probability + calibration error + persistence; the earned/gated/
self-disabling influence weight; that learning actually changes the directional probability; and
PROOF that learning can never bypass the execution-quality gate or enable live trading.
"""

from __future__ import annotations

import math
import random

from engine.pulse.edge_model import EdgeModel, FEATURE_NAMES
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


def _vec(z: float) -> dict:
    v = {f: 0.0 for f in FEATURE_NAMES}
    v["zscore"] = z
    return v


def _calibrated_model(min_samples: int = 20, n: int = 240, seed: int = 0) -> EdgeModel:
    """Train on a genuinely calibratable logistic pattern: P(up) = sigmoid(zscore), outcome ~
    Bernoulli(P) over a range of zscore values. A logistic model learns this and is well
    calibrated (low ECE), unlike a deterministic separable pattern (pathologically under-confident)."""
    m = EdgeModel(min_samples=min_samples, lr=0.1)
    rnd = random.Random(seed)
    for _ in range(n):
        z = rnd.uniform(-3.0, 3.0)
        p = 1.0 / (1.0 + math.exp(-z))
        m.observe_label(_vec(z), outcome_up=(rnd.random() < p))
    return m


# ------------------------------- edge model primitives ------------------------------------- #
def test_decision_p_up_learns_direction():
    m = _calibrated_model()
    assert m.decision_p_up(_vec(1.0)) > 0.5      # learned: positive zscore -> up
    assert m.decision_p_up(_vec(-1.0)) < 0.5
    assert EdgeModel().decision_p_up(_vec(1.0)) is None   # never learned -> None


def test_calibration_error_low_when_calibrated():
    m = _calibrated_model()
    ece = m.calibration_error()
    assert ece is not None and ece <= 0.15       # genuinely calibrated -> low ECE


def test_edge_model_persists_round_trip():
    m = _calibrated_model()
    m2 = EdgeModel(min_samples=20)
    m2.load_state(m.to_state())
    assert m2.n_labeled == m.n_labeled
    assert abs(m2.decision_p_up(_vec(1.0)) - m.decision_p_up(_vec(1.0))) < 1e-9
    assert m2.calibration_error() == m.calibration_error()


# ------------------------------- influence weight gating ----------------------------------- #
def _engine(tmp_path, **cfg_over):
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9, data_dir=str(tmp_path),
                      **cfg_over)
    feed = PulsePriceFeed(fetcher=lambda: 64000.0, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    return PulseEngine(cfg, market_feed=_DeepMkt(), price_feed=feed)


class _DeepMkt:
    def active_windows(self, now=None, **kw):
        return []

    def hydrate_books(self, w):
        return w

    def fetch_resolution(self, market_id):
        return True


def test_weight_disabled_by_default(tmp_path):
    eng = _engine(tmp_path)                       # learning_enabled defaults False
    assert eng._learning_weight() == (0.0, "disabled")
    assert eng._learning_report()["active"] is False


def test_weight_earned_gated_and_self_disabling(tmp_path):
    eng = _engine(tmp_path, learning_enabled=True, learning_min_samples=40,
                  learning_max_weight=0.5, learning_ramp_samples=200,
                  learning_max_calib_error=0.15)
    # untrained -> no influence
    assert eng._learning_weight() == (0.0, "insufficient_samples")
    # a calibrated model partway up the ramp -> active, weight below the cap
    eng.edge_model = _calibrated_model(min_samples=40, n=120)
    w1, why1 = eng._learning_weight()
    assert why1 == "active" and 0 < w1 < 0.5
    # far more samples -> the weight grows and caps at max_weight (earned influence)
    eng.edge_model = _calibrated_model(min_samples=40, n=400)
    w2, _ = eng._learning_weight()
    assert w2 > w1 and w2 == 0.5
    # a MISCALIBRATED model self-disables (calibration error above the cap)
    eng.cfg.learning_max_calib_error = 0.0
    assert eng._learning_weight() == (0.0, "calibration_degraded")


# ------------------------------- learning changes the decision ----------------------------- #
class _Mkt:
    def __init__(self, w, *, deep=True):
        self._w = w
        self._deep = deep

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        if self._deep:
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=50000,
                                  bid_depth_usd=50000, asks=[(0.55, 100000.0)],
                                  bids=[(0.50, 100000.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=49000,
                                    bid_depth_usd=44000, asks=[(0.49, 100000.0)],
                                    bids=[(0.44, 100000.0)])
        else:   # thin -> execution gate must reject (partial_fill_risk) regardless of learning
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=2.0,
                                  bid_depth_usd=2.0, asks=[(0.55, 1.0)], bids=[(0.50, 1.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=2.0,
                                    bid_depth_usd=2.0, asks=[(0.49, 1.0)], bids=[(0.44, 1.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _run(tmp_path, *, deep, learning):
    t0 = 9_900_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += (3.0 if (int(price["p"]) % 2 == 0) else -1.0)   # wiggle -> sigma>0, ~flat
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
                      directional_down_only=False, directional_block_up_until_promoted=False,
                      directional_up_restrictions_enabled=False,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9, data_dir=str(tmp_path),
                      learning_enabled=learning, learning_min_samples=20, learning_max_weight=0.5,
                      learning_ramp_samples=20, learning_max_calib_error=0.95)
    eng = PulseEngine(cfg, market_feed=_Mkt(win, deep=deep), price_feed=feed)
    if learning:                       # pre-train a strong UP bias (all-zero entry vec -> bias)
        for _ in range(120):
            eng.edge_model.observe_label({f: 0.0 for f in FEATURE_NAMES}, outcome_up=True)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    return eng


def test_learning_blend_is_applied_and_correct(tmp_path):
    eng = _run(tmp_path, deep=True, learning=True)
    st = eng.status()
    lr = st["learning"]
    assert lr["enabled"] is True and lr["active"] is True and lr["weight"] > 0
    applied = [r["learning"] for r in st["recent_evaluations"]
               if r.get("learning") and r["learning"].get("applied")]
    assert applied, "learning should have adjusted at least one candidate's probability"
    b = applied[0]
    # blend math: blended == (1-w)*digital + w*model (within the [0.01,0.99] clamp)
    expect = (1 - b["weight"]) * b["digital_p_up"] + b["weight"] * b["model_p_up"]
    assert abs(b["blended_p_up"] - max(0.01, min(0.99, expect))) < 1e-3
    assert b["gate_still_authoritative"] is True and b["paper_only"] is True


def test_learning_off_does_not_touch_decision(tmp_path):
    st = _run(tmp_path, deep=True, learning=False).status()
    assert st["learning"]["enabled"] is False and st["learning"]["active"] is False
    assert not [r for r in st["recent_evaluations"]
                if r.get("learning") and r["learning"].get("applied")]


def test_learning_cannot_bypass_execution_gate(tmp_path):
    # thin book + strong learned UP bias -> the gate MUST still reject every candidate
    eng = _run(tmp_path, deep=False, learning=True)
    assert eng.ledger.trades == 0
    eg = eng.ledger.exec_gate_stats()
    assert eg["candidates"] >= 1 and eg["accepted"] == 0
    assert eg["rejected"]["partial_fill_risk"] >= 1 and eg["reconciled"] is True
    # learning was active, yet it produced no trade and live trading stays off
    assert eng._learning_report()["active"] is True
    assert eng.status()["live_trading_enabled"] is False
    assert eng.light_report()["global_reconciled"] is True
