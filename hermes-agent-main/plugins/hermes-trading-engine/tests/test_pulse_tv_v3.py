"""TradingView Composite v3 schema: ADX, SuperTrend, candle pressure, range breakout, MTF align.

Proves: valid v3 parsing, missing-optional safety, invalid-enum coercion to "unknown", the v3
report buckets, v2 backward compatibility, and that v3 cannot bypass the execution gate.
"""

from __future__ import annotations

import json

from engine.pulse.tradingview import (TradingViewIntake, TradingViewSignalLearner,
                                       ADX_STATES, SUPERTREND_DIRECTIONS, CANDLE_PRESSURES,
                                       RANGE_STATES, MTF_ALIGNMENTS)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig

SECRET = "s3cr3t-token"


def _intake():
    return TradingViewIntake(secret=SECRET, allowed_symbols=["BTCUSD"], bot_name="hermes")


def _alert(**over):
    base = {"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD", "direction": "UP",
            "event_id": "v3-1"}
    base.update(over)
    return json.dumps(base).encode()


def test_v3_fields_parsed():
    intake = _intake()
    code, body = intake.ingest(_alert(
        adx=31.2, adx_state="strong_trend", supertrend_value=63100.0,
        supertrend_direction="bullish", supertrend_aligned=True,
        candle_pressure="bull_close_near_high", body_ratio=0.72, close_position=0.94,
        upper_wick_ratio=0.05, lower_wick_ratio=0.23, range_state="breakout_up",
        range_lookback=20, prior_range_high=63200.0, prior_range_low=62800.0,
        mtf_alignment="bullish_aligned", bar_confirmed=True, signal_age_ms=120,
        non_repainting=True, composite_version="v3"), now=1_000_000.0)
    assert code == 200 and body["accepted"] is True
    ev = intake.latest
    assert ev.adx == 31.2 and ev.adx_state == "strong_trend"
    assert ev.supertrend_value == 63100.0 and ev.supertrend_direction == "bullish"
    assert ev.supertrend_aligned is True and ev.candle_pressure == "bull_close_near_high"
    assert ev.body_ratio == 0.72 and ev.close_position == 0.94
    assert ev.range_state == "breakout_up" and ev.prior_range_high == 63200.0
    assert ev.mtf_alignment == "bullish_aligned" and ev.bar_confirmed is True
    assert ev.signal_age_ms == 120 and ev.non_repainting is True
    assert ev.composite_version == "v3"
    feat = ev.as_feature(now=1_000_000.0)
    for k in ("adx", "adx_state", "supertrend_direction", "candle_pressure", "range_state",
              "mtf_alignment", "bar_confirmed", "non_repainting"):
        assert k in feat
    assert ev.adx_state in ADX_STATES and ev.supertrend_direction in SUPERTREND_DIRECTIONS
    assert ev.candle_pressure in CANDLE_PRESSURES and ev.range_state in RANGE_STATES
    assert ev.mtf_alignment in MTF_ALIGNMENTS


def test_v3_missing_fields_safe():
    intake = _intake()
    code, body = intake.ingest(_alert(event_id="v3-missing"), now=1_000_000.0)   # no v3 fields
    assert code == 200 and body["accepted"] is True
    ev = intake.latest
    assert ev.adx_state == "unknown" and ev.supertrend_direction == "unknown"
    assert ev.candle_pressure == "unknown" and ev.range_state == "unknown"
    assert ev.mtf_alignment == "unknown"
    assert ev.adx is None and ev.supertrend_value is None and ev.bar_confirmed is None
    assert ev.non_repainting is None and ev.signal_age_ms is None


def test_v3_invalid_enums_coerced():
    intake = _intake()
    code, body = intake.ingest(_alert(
        adx_state="mega_trend", supertrend_direction="up", candle_pressure="doji",
        range_state="sideways", mtf_alignment="kinda", adx="not-a-number",
        bar_confirmed="maybe", event_id="v3-bad"), now=1_000_000.0)
    assert code == 200 and body["accepted"] is True       # bad enums never reject the alert
    ev = intake.latest
    assert ev.adx_state == "unknown" and ev.supertrend_direction == "unknown"
    assert ev.candle_pressure == "unknown" and ev.range_state == "unknown"
    assert ev.mtf_alignment == "unknown"
    assert ev.adx is None and ev.bar_confirmed is None     # bad numeric/bool -> None


def test_v3_learner_buckets_and_v2_backward_compat():
    L = TradingViewSignalLearner()
    tags = {"direction": "UP", "vwap_state": "above", "composite_version": "v3",   # v2 dims preserved
            "adx_state": "strong_trend", "supertrend_direction": "bullish",
            "candle_pressure": "bull_close_near_high", "range_state": "breakout_up",
            "mtf_alignment": "bullish_aligned"}
    for i in range(10):
        L.record_settled(tags, won=(i % 2 == 0), pnl=(2.0 if i % 2 == 0 else -5.0),
                         ev_after_cost=0.03, reconciled=True)
    rep = L.report()
    for fld in ("by_adx_state", "by_supertrend_direction", "by_candle_pressure",
                "by_range_state", "by_mtf_alignment", "by_composite_version",
                "by_vwap_state", "by_signal_level"):                # v2 + v3 both present
        assert fld in rep, fld
    for dim in ("adx_state", "supertrend_direction", "candle_pressure", "range_state",
                "mtf_alignment"):
        assert sum(b["n"] for b in rep["by_" + dim].values()) == 10, dim
    assert rep["by_adx_state"]["strong_trend"]["n"] == 10
    assert rep["by_range_state"]["breakout_up"]["win_rate"] == 0.5


# ------------------------------- engine: reconcile + cannot bypass gate -------------------- #
class _Mkt:
    def __init__(self, w, *, deep):
        self._w, self._deep = w, deep

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
        else:
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=2.0, bid_depth_usd=2.0,
                                  asks=[(0.55, 1.0)], bids=[(0.50, 1.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=2.0, bid_depth_usd=2.0,
                                    asks=[(0.49, 1.0)], bids=[(0.44, 1.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _engine(tmp_path, *, deep):
    t0 = 9_920_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                      tradingview_secret=SECRET, tradingview_webhook_port=0,
                      tradingview_allowed_symbols=("BTC/USD", "BTCUSD"), data_dir=str(tmp_path))
    return PulseEngine(cfg, market_feed=_Mkt(win, deep=deep), price_feed=feed), t0


def _v3_alert():
    return json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTC/USD",
                       "direction": "UP", "adx": 30.0, "adx_state": "strong_trend",
                       "supertrend_direction": "bullish", "candle_pressure": "bull_close_near_high",
                       "range_state": "breakout_up", "mtf_alignment": "bullish_aligned",
                       "composite_version": "v3", "event_id": "v3-eng"}).encode()


def test_engine_v3_buckets_reconcile_with_ledger(tmp_path):
    eng, t0 = _engine(tmp_path, deep=True)
    eng.tradingview.ingest(_v3_alert(), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)
    sl = eng.status()["tradingview"]["signal_learning"]
    tv_settled = sum(1 for p in eng.ledger.positions.values()
                     if p.status == "settled" and (p.external or {}).get("source") == "tradingview")
    assert sl["settled_with_signal"] == tv_settled >= 1
    for dim in ("adx_state", "supertrend_direction", "candle_pressure", "range_state",
                "mtf_alignment"):
        assert sum(b["n"] for b in sl["by_" + dim].values()) == sl["settled_with_signal"]
    assert sl["by_adx_state"].get("strong_trend", {}).get("n") == sl["settled_with_signal"]
    assert eng.light_report()["global_reconciled"] is True


def test_engine_v3_cannot_bypass_gate(tmp_path):
    eng, t0 = _engine(tmp_path, deep=False)
    eng.tradingview.ingest(_v3_alert(), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    assert eng.ledger.trades == 0
    assert eng.ledger.exec_gate_stats()["rejected"]["partial_fill_risk"] >= 1
    assert eng.status()["live_trading_enabled"] is False
