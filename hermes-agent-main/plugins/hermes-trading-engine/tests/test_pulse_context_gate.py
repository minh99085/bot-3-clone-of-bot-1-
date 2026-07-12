"""TradingView Context Gate — hard-prior restrict-only block of proven-losing contexts (PAPER ONLY).

Proves: spike/noise/far-ttc contexts are blocked; clean contexts pass; the gate is OFF by default;
exploration is hard-capped and separated; state round-trips; and end-to-end the gate can only make
the bot MORE selective (it never forces a trade and the execution gate stays authoritative), while
reconciliation still holds.
"""

from __future__ import annotations

import json as _json

from engine.pulse.context_gate import TradingViewContextGate
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


# ------------------------------- pure gate rules ------------------------------------------ #
def test_blocks_volume_spike():
    g = TradingViewContextGate(enabled=True, blocked_hurst_regimes=(), max_ttc_s=None,
                               exploration_rate=0.0)
    r = g.evaluate(volume_state="spike", hurst_regime="trending", ttc_s=120)
    assert r["decision"] == "block" and r["reasons"] == ["tv_context_volume_spike"]
    assert g.block_reasons["tv_context_volume_spike"] == 1
    # a non-blocked volume state passes
    assert g.evaluate(volume_state="active", hurst_regime="trending", ttc_s=120)["decision"] == "pass"


def test_blocks_noise_regime():
    g = TradingViewContextGate(enabled=True, blocked_volume_states=(), max_ttc_s=None,
                               exploration_rate=0.0)
    assert g.evaluate(hurst_regime="noise", ttc_s=60)["reasons"] == ["tv_context_hurst_noise"]
    assert g.evaluate(hurst_regime="mean_reverting", ttc_s=60)["decision"] == "pass"


def test_blocks_liquidation_spike():
    g = TradingViewContextGate(enabled=True, blocked_volume_states=(), blocked_hurst_regimes=(),
                               max_ttc_s=None, block_liquidation_spike=True, exploration_rate=0.0)
    assert g.evaluate(liquidation_spike=True)["reasons"] == ["tv_context_liquidation_spike"]
    assert g.evaluate(liquidation_spike=False)["decision"] == "pass"


def test_blocks_event_blackout():
    g = TradingViewContextGate(enabled=True, blocked_volume_states=(), blocked_hurst_regimes=(),
                               max_ttc_s=None, block_event_blackout=True, exploration_rate=0.0)
    assert g.evaluate(event_blackout=True)["reasons"] == ["tv_context_event_blackout"]


def test_blocks_grok_event_risk_high():
    g = TradingViewContextGate(enabled=True, blocked_volume_states=(), blocked_hurst_regimes=(),
                               max_ttc_s=None, block_grok_event_risk_high=True, exploration_rate=0.0)
    assert g.evaluate(grok_event_risk="high")["reasons"] == ["tv_context_grok_event_risk_high"]
    assert g.evaluate(grok_event_risk="low")["decision"] == "pass"


def test_blocks_far_ttc():
    g = TradingViewContextGate(enabled=True, blocked_volume_states=(), blocked_hurst_regimes=(),
                               max_ttc_s=240.0, exploration_rate=0.0)
    assert g.evaluate(ttc_s=250)["reasons"] == ["tv_context_ttc_too_far"]
    assert g.evaluate(ttc_s=239)["decision"] == "pass"
    # ttc rule can be disabled with max_ttc_s=None
    g2 = TradingViewContextGate(enabled=True, blocked_volume_states=(), blocked_hurst_regimes=(),
                                max_ttc_s=None)
    assert g2.evaluate(ttc_s=10_000)["decision"] == "pass"


def test_disabled_passes_everything():
    g = TradingViewContextGate(enabled=False)
    r = g.evaluate(volume_state="spike", hurst_regime="noise", ttc_s=9999)
    assert r["decision"] == "pass" and r["active"] is False
    assert g.blocked == 0 and g.passed == 0           # disabled gate keeps no headline counters


def test_exploration_capped_and_separated():
    g = TradingViewContextGate(enabled=True, blocked_volume_states=("spike",),
                               blocked_hurst_regimes=(), max_ttc_s=None,
                               exploration_rate=0.5, seed=11)
    assert g.exploration_rate == 0.05                  # hard cap regardless of requested 0.5
    blk = exp = 0
    for _ in range(2000):
        d = g.evaluate(volume_state="spike", ttc_s=60)["decision"]
        blk += int(d == "block")
        exp += int(d == "explore")
    assert exp > 0 and blk > 0
    assert 0.02 < exp / (exp + blk) < 0.08            # ~5% exploration carve-out
    rep = g.report()
    assert rep["blocked"] == blk and rep["explored"] == exp
    assert rep["explore_reasons"]["tv_context_volume_spike"] == exp


def test_state_roundtrip():
    g = TradingViewContextGate(enabled=True, blocked_hurst_regimes=(), max_ttc_s=None,
                               exploration_rate=0.0)
    g.evaluate(volume_state="spike", ttc_s=60)
    g.evaluate(volume_state="active", ttc_s=60)
    g2 = TradingViewContextGate(enabled=True)
    g2.load_state(g.to_state())
    assert g2.blocked == g.blocked == 1 and g2.passed == g.passed == 1
    assert g2.block_reasons == g.block_reasons


# ============================ engine end-to-end =========================================== #
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


def _engine(tmp_path, *, deep=True, **over):
    t0 = 9_960_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                      tv_context_exploration_rate=0.0, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False, data_dir=str(tmp_path), **over)
    return PulseEngine(cfg, market_feed=_Mkt(win, deep=deep), price_feed=feed), t0


def _drive(eng, t0):
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)


def test_engine_blocks_far_ttc_context(tmp_path):
    # only the ttc rule is active; every driven tick has ttc>=240 -> all blocked by context gate
    eng, t0 = _engine(tmp_path, deep=True, tv_context_gate_enabled=True,
                      tv_context_blocked_volume_states=(), tv_context_blocked_hurst_regimes=(),
                      tv_context_max_ttc_s=240.0)
    _drive(eng, t0)
    assert eng.ledger.trades == 0
    lc = eng.status()["decision_lifecycle"]
    assert lc["rejected_by_stage"].get("context_gate", 0) >= 1
    cg = eng.status()["tradingview"]["context_gate"]
    assert cg["enabled"] is True and cg["blocked"] >= 1
    assert cg["block_reasons"].get("tv_context_ttc_too_far", 0) >= 1
    assert eng.light_report()["global_reconciled"] is True
    assert eng.status()["live_trading_enabled"] is False and eng.status()["paper_only"] is True


def test_engine_blocks_tradingview_volume_spike(tmp_path):
    # only the volume rule active (ttc rule disabled); a spike TradingView signal -> blocked
    eng, t0 = _engine(tmp_path, deep=True, tv_context_gate_enabled=True,
                      tv_context_blocked_volume_states=("spike",),
                      tv_context_blocked_hurst_regimes=(), tv_context_max_ttc_s=None,
                      tradingview_secret="s3cr3t", tradingview_webhook_port=0,
                      tradingview_allowed_symbols=("BTC/USD",))
    eng.tradingview.ingest(_json.dumps({"secret": "s3cr3t", "bot_name": "hermes",
                                        "symbol": "BTC/USD", "direction": "UP",
                                        "volume_state": "spike", "event_id": "tv1"}).encode(),
                           now=t0 - 6)
    _drive(eng, t0)
    assert eng.ledger.trades == 0
    cg = eng.status()["tradingview"]["context_gate"]
    assert cg["block_reasons"].get("tv_context_volume_spike", 0) >= 1
    assert eng.light_report()["global_reconciled"] is True
    if eng.webhook is not None:
        eng.webhook.stop()


def test_engine_clean_context_passes_and_trades(tmp_path):
    # gate enabled but no rule can match (all rules emptied) -> behaves like before, trades normally
    eng, t0 = _engine(tmp_path, deep=True, tv_context_gate_enabled=True,
                      tv_context_blocked_volume_states=(), tv_context_blocked_hurst_regimes=(),
                      tv_context_max_ttc_s=None)
    _drive(eng, t0)
    assert eng.ledger.trades >= 1
    cg = eng.status()["tradingview"]["context_gate"]
    assert cg["enabled"] is True and cg["passed"] >= 1 and cg["blocked"] == 0
    assert eng.light_report()["global_reconciled"] is True


def test_engine_context_gate_off_by_default(tmp_path):
    eng, t0 = _engine(tmp_path, deep=True)            # no override -> default OFF
    _drive(eng, t0)
    cg = eng.status()["tradingview"]["context_gate"]
    assert cg["enabled"] is False
    assert eng.ledger.trades >= 1                      # default behavior unchanged
