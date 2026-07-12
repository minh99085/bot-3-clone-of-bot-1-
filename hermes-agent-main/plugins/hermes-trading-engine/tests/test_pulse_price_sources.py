"""Pulse price sources: Chainlink Data Streams auth/decode, source selection, sampler."""

from __future__ import annotations

import hashlib
import hmac
import time

from engine.pulse.chainlink_streams import (sign_headers, decode_v3_price, available,
                                             chainlink_streams_fetcher)
from engine.pulse.price import build_price_source, PulsePriceFeed


def test_hmac_signature_matches_documented_format():
    # string-to-sign = "METHOD FULL_PATH BODY_HASH API_KEY TIMESTAMP" (single spaces)
    key, secret, path, ts = "the-uuid-key", "the-secret", "/api/v1/reports/latest?feedID=0xabc", 1716211845123
    h = sign_headers(key, secret, "get", path, ts_ms=ts)
    body_hash = hashlib.sha256(b"").hexdigest()
    expected_str = f"GET {path} {body_hash} {key} {ts}"
    expected_sig = hmac.new(secret.encode(), expected_str.encode(), hashlib.sha256).hexdigest()
    assert h["Authorization"] == key
    assert h["X-Authorization-Timestamp"] == str(ts)
    assert h["X-Authorization-Signature-SHA256"] == expected_sig


def test_decode_v3_price_round_trip():
    # build a minimal v3 fullReport: head(7 words) + reportData(len + 9 words, price at idx 6)
    price_int = int(64123.5 * 1e18)
    context = b"\x00" * 96                       # words 0,1,2
    w3 = (224).to_bytes(32, "big")               # offset to reportData (after 7-word head)
    rest_head = b"\x00" * 96                      # words 4,5,6 (rs/ss offsets + rawVs)
    head = context + w3 + rest_head              # 224 bytes
    words = [b"\x00" * 32] * 9
    words[6] = price_int.to_bytes(32, "big", signed=True)
    report_data = b"".join(words)                # 288 bytes
    full = head + len(report_data).to_bytes(32, "big") + report_data
    assert abs(decode_v3_price("0x" + full.hex()) - 64123.5) < 1e-6
    assert decode_v3_price("0xdeadbeef") is None     # garbage -> fail-open None


def test_source_selection_and_fallback(monkeypatch):
    for k in ("CHAINLINK_DATA_STREAMS_API_KEY", "CHAINLINK_DATA_STREAMS_SECRET",
              "CHAINLINK_BTC_FEED_ID"):
        monkeypatch.delenv(k, raising=False)
    assert build_price_source("coinbase")[1] == "coinbase"
    assert build_price_source("pyth")[1] == "pyth"
    assert build_price_source("auto")[1] == "coinbase"          # no creds -> proxy
    assert build_price_source("chainlink")[1] == "coinbase"     # no creds -> fallback
    assert available() is False
    # with creds present, auto/chainlink select the exact Data Streams feed
    monkeypatch.setenv("CHAINLINK_DATA_STREAMS_API_KEY", "uuid")
    monkeypatch.setenv("CHAINLINK_DATA_STREAMS_SECRET", "secret")
    monkeypatch.setenv("CHAINLINK_BTC_FEED_ID", "0xabc")
    assert available() is True
    assert build_price_source("auto")[1] == "chainlink_data_streams"
    assert build_price_source("chainlink")[1] == "chainlink_data_streams"


def test_chainlink_fetcher_failopen_without_creds():
    f = chainlink_streams_fetcher(api_key="", secret="", feed_id="")
    assert f() is None                            # no creds -> None (engine uses proxy)


def test_background_sampler_keeps_price_fresh():
    feed = PulsePriceFeed(fetcher=lambda: 64000.0, source_name="test",
                          sampler_interval_s=0.05)
    feed.start_sampler()
    try:
        time.sleep(0.3)
        assert feed.polls > 0 and feed.current() == 64000.0
        assert feed.status()["sampler_running"] is True
    finally:
        feed.stop_sampler()
