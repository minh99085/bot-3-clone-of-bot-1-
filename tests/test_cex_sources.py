"""Multi-venue CEX source layer — parsers, rotation, circuit breaker.

No Binance anywhere (geo-blocked 451 on the VPS). All HTTP mocked via the
_get_json seam.
"""

from __future__ import annotations

import time

import pytest

import connectors.cex_sources as cs


@pytest.fixture(autouse=True)
def _reset_health(monkeypatch):
    cs._HEALTH.clear()
    monkeypatch.delenv("HERMES_CEX_SOURCES", raising=False)
    yield
    cs._HEALTH.clear()


def _fake_get_json(responses):
    """responses: {url_substring: payload_or_Exception}"""
    def fake(url, params=None):
        for key, payload in responses.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected url {url}")
    return fake


def test_no_binance_endpoints_anywhere():
    """No Binance URLs/hosts may remain in the price path (prose mentions in
    docstrings explaining the removal are fine)."""
    import inspect

    import connectors.cex_realtime as cx

    for mod in (cs, cx):
        src = inspect.getsource(mod).lower()
        assert "binance.com" not in src
        assert "fstream.binance" not in src
        assert "bybit.com" not in src


def test_coinbase_mid_parse(monkeypatch):
    monkeypatch.setattr(cs, "_get_json", _fake_get_json({
        "coinbase.com/products/BTC-USD/ticker": {"bid": "64000", "ask": "64010", "price": "64005"},
    }))
    q = cs._mid_coinbase("BTC")
    assert q.mid == pytest.approx(64005.0) and q.source == "coinbase"


def test_kraken_mid_parse(monkeypatch):
    monkeypatch.setattr(cs, "_get_json", _fake_get_json({
        "kraken.com/0/public/Ticker": {
            "result": {"XXBTZUSD": {"b": ["64000.1", "1"], "a": ["64010.5", "1"], "c": ["64007", "0.1"]}}
        },
    }))
    q = cs._mid_kraken("BTC")
    assert q.mid == pytest.approx((64000.1 + 64010.5) / 2) and q.source == "kraken"


def test_bitstamp_mid_parse(monkeypatch):
    monkeypatch.setattr(cs, "_get_json", _fake_get_json({
        "bitstamp.net/api/v2/ticker/btcusd": {"bid": "63990", "ask": "64020", "last": "64000"},
    }))
    q = cs._mid_bitstamp("BTC")
    assert q.mid == pytest.approx(64005.0) and q.source == "bitstamp"


def test_okx_mid_parse(monkeypatch):
    monkeypatch.setattr(cs, "_get_json", _fake_get_json({
        "okx.com/api/v5/market/ticker": {
            "data": [{"bidPx": "64001", "askPx": "64009", "last": "64004"}]
        },
    }))
    q = cs._mid_okx("BTC")
    assert q.mid == pytest.approx(64005.0) and q.source == "okx"


def test_rotation_skips_failing_source(monkeypatch):
    monkeypatch.setattr(cs, "_get_json", _fake_get_json({
        "coinbase.com": RuntimeError("451"),
        "kraken.com": {"result": {"XXBTZUSD": {"b": ["100", "1"], "a": ["102", "1"], "c": ["101", "1"]}}},
    }))
    quotes = cs.get_mid_multi("BTC", k=1)
    assert len(quotes) == 1 and quotes[0].source == "kraken"


def test_circuit_breaker_cools_down_failing_source(monkeypatch):
    calls = {"coinbase": 0}

    def fake(url, params=None):
        if "coinbase.com" in url:
            calls["coinbase"] += 1
            raise RuntimeError("429")
        if "kraken.com" in url:
            return {"result": {"X": {"b": ["100", "1"], "a": ["102", "1"], "c": ["101", "1"]}}}
        raise AssertionError(url)

    monkeypatch.setattr(cs, "_get_json", fake)
    for _ in range(cs.FAILS_TO_TRIP):
        cs.get_mid("BTC")
    tripped_calls = calls["coinbase"]
    assert tripped_calls == cs.FAILS_TO_TRIP
    # Now cooling down: further calls must NOT hit coinbase at all
    cs.get_mid("BTC")
    assert calls["coinbase"] == tripped_calls
    h = cs._health("coinbase")
    assert not h.available()
    # After cooldown expires it becomes available again
    h.cooldown_until = time.time() - 1
    assert h.available()


def test_env_order_override(monkeypatch):
    monkeypatch.setenv("HERMES_CEX_SOURCES", "kraken,coinbase")
    assert cs.source_order() == ["kraken", "coinbase"]
    monkeypatch.setenv("HERMES_CEX_SOURCES", "nonsense")
    assert cs.source_order() == list(cs.DEFAULT_ORDER)


def test_kline_open_fallback_chain(monkeypatch):
    ts = 1_784_475_000
    monkeypatch.setattr(cs, "_get_json", _fake_get_json({
        "coinbase.com/products/BTC-USD/candles": [],  # no candle
        "kraken.com/0/public/OHLC": {
            "result": {"XXBTZUSD": [[ts, "64100.5", "64150", "64050", "64120", "64110", "3", 42]], "last": ts}
        },
    }))
    px = cs.kline_open_at("BTC", ts)
    assert px == pytest.approx(64100.5)


def test_kline_none_when_all_sources_lack_candle(monkeypatch):
    monkeypatch.setattr(cs, "_get_json", _fake_get_json({
        "coinbase.com": [],
        "kraken.com": {"result": {"last": 0}},
        "bitstamp.net": {"data": {"ohlc": []}},
        "okx.com": {"data": []},
    }))
    assert cs.kline_open_at("BTC", 1_784_475_000) == 0.0


def test_realtime_feed_uses_sources(monkeypatch):
    import connectors.cex_realtime as cx

    monkeypatch.setattr(cs, "_get_json", _fake_get_json({
        "coinbase.com/products/BTC-USD/ticker": {"bid": "64000", "ask": "64010", "price": "64005"},
        "kraken.com/0/public/Ticker": {
            "result": {"X": {"b": ["64002", "1"], "a": ["64012", "1"], "c": ["64007", "1"]}}
        },
    }))
    feed = cx.RealtimeBtcFeed()
    feed._refresh_rest()  # no thread in tests
    snap = feed.get_snapshot()
    assert snap.mid == pytest.approx(64005.0)
    assert snap.binance.source == "coinbase"  # legacy field = primary venue
    assert snap.bybit.source == "kraken"      # legacy field = secondary venue
    assert snap.sources_agree  # 2bps apart < 15bps
    feed.stop()
