"""TradingView webhook listener startup + schema robustness.

Proves: the listener starts automatically when TRADINGVIEW_WEBHOOK_SECRET is set (and not
otherwise), emits the exact startup log, exposes listener status (incl. in the light report),
accepts exchange-prefixed symbol aliases, honors BOT_NAME, and that TradingView stays observe-only
and cannot bypass the execution gate.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from engine.pulse.tradingview import TradingViewIntake, BAD_SECRET, UNSUPPORTED_SYMBOL
from engine.pulse.webhook import WebhookServer
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig

SECRET = "s3cr3t-token"


# ------------------------------- symbol aliases (req #2) ----------------------------------- #
def test_symbol_aliases_accepted():
    intake = TradingViewIntake(secret=SECRET,
                               allowed_symbols=["BTCUSD", "INDEX:BTCUSD"], bot_name="hermes")
    for sym, exp in (("INDEX:BTCUSD", "BTCUSD"), ("BTCUSD", "BTCUSD")):
        code, body = intake.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes",
                                               "symbol": sym, "direction": "UP",
                                               "event_id": "a-" + sym}).encode(), now=1e6)
        assert code == 200 and body["accepted"] is True, sym
        assert intake.latest.symbol == exp
    code, body = intake.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes",
                                           "symbol": "BINANCE:BTCUSDT", "direction": "UP",
                                           "event_id": "bn"}).encode(), now=1e6)
    assert code == 400 and body["reason"] == UNSUPPORTED_SYMBOL
    # an unrelated symbol is still rejected
    code, body = intake.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes",
                                           "symbol": "ETHUSD", "direction": "UP",
                                           "event_id": "eth"}).encode(), now=1e6)
    assert code == 400 and body["reason"] == UNSUPPORTED_SYMBOL


# ------------------------------- listener startup + status + log (req #1,#3) --------------- #
def test_listener_startup_status_and_log(caplog):
    intake = TradingViewIntake(secret=SECRET, allowed_symbols=["BTCUSD"], bot_name="hermes")
    with caplog.at_level(logging.INFO, logger="hte.pulse.webhook"):
        srv = WebhookServer(intake, host="127.0.0.1", port=0,
                            path="/webhooks/tradingview").start()
    try:
        st = srv.status()
        assert st["listening"] is True and st["observe_only"] is True
        assert st["path"] == "/webhooks/tradingview" and st["bound_internal"] is True
        # exact startup log format
        assert any("TradingView webhook listening host=" in r.message
                   and "observe_only=true" in r.message for r in caplog.records)
        # health endpoint responds
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/health", timeout=5) as r:
            assert r.status == 200
    finally:
        srv.stop()


# ------------------------------- engine auto-start + BOT_NAME (req #1,#2) ------------------ #
def _mini_engine(tmp_path, **cfg_over):
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=9e9, close_ts=9e9 + 300, up_token_id="U", down_token_id="D")

    class _Mkt:
        def active_windows(self, now=None, **kw):
            return [win]

        def hydrate_books(self, w):
            return w

        def fetch_resolution(self, market_id):
            return True

    feed = PulsePriceFeed(fetcher=lambda: 64000.0, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, tradingview_webhook_port=0, data_dir=str(tmp_path),
                      **cfg_over)
    return PulseEngine(cfg, market_feed=_Mkt(), price_feed=feed)


def test_engine_autostarts_webhook_only_when_secret_set(tmp_path):
    # no secret -> no listener
    eng0 = _mini_engine(tmp_path, tradingview_secret="")
    assert eng0.webhook is None and eng0.tradingview is None
    rep0 = eng0.light_report()["tradingview"]["webhook"]
    assert rep0["listening"] is False                      # listener status present in light report
    # secret set -> listener auto-starts and is observe-only
    eng = _mini_engine(tmp_path, tradingview_secret=SECRET)
    assert eng.webhook is not None and eng.webhook.status()["listening"] is True
    rep = eng.light_report()["tradingview"]["webhook"]
    assert rep["listening"] is True and rep["observe_only"] is True
    eng.webhook.stop()


def test_bot_name_env_fallback(monkeypatch):
    monkeypatch.delenv("TRADINGVIEW_BOT_NAME", raising=False)
    monkeypatch.setenv("BOT_NAME", "hermes")
    assert PulseConfig.from_env().tradingview_bot_name == "hermes"
    monkeypatch.setenv("TRADINGVIEW_BOT_NAME", "specific")
    assert PulseConfig.from_env().tradingview_bot_name == "specific"   # explicit wins over BOT_NAME


# ------------------------------- bad secret + cannot bypass gate (req #6,#7,#8) ------------ #
def test_bad_secret_sanitized_400_no_secret_leak():
    intake = TradingViewIntake(secret=SECRET, allowed_symbols=["BTCUSD"], bot_name="hermes")
    code, body = intake.ingest(json.dumps({"secret": "WRONG", "bot_name": "hermes",
                                           "symbol": "BTCUSD", "direction": "UP",
                                           "event_id": "b"}).encode(), now=1e6)
    assert code == 401 and body["reason"] == BAD_SECRET
    assert SECRET not in json.dumps(body)                  # never echoes the real secret


def test_tradingview_cannot_bypass_gate_thin_book(tmp_path):
    t0 = 9_910_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")

    class _ThinMkt:
        def active_windows(self, now=None, **kw):
            return [win]

        def hydrate_books(self, w):
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=2.0, bid_depth_usd=2.0,
                                  asks=[(0.55, 1.0)], bids=[(0.50, 1.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=2.0, bid_depth_usd=2.0,
                                    asks=[(0.49, 1.0)], bids=[(0.44, 1.0)])
            return w

        def fetch_resolution(self, market_id):
            return True

    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                      tradingview_secret=SECRET, tradingview_webhook_port=0,
                      tradingview_allowed_symbols=("BTC/USD", "BTCUSD"), data_dir=str(tmp_path))
    eng = PulseEngine(cfg, market_feed=_ThinMkt(), price_feed=feed)
    eng.tradingview.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTC/USD",
                                       "direction": "UP", "vwap_state": "above", "htf_bias": "bullish",
                                       "event_id": "tv"}).encode(), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    assert eng.ledger.trades == 0
    assert eng.status()["live_trading_enabled"] is False
    if eng.webhook is not None:
        eng.webhook.stop()
