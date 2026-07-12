"""TV symbol mapping + intrahour timeframe tracking."""

from __future__ import annotations

import json

from engine.pulse.tradingview import tv_symbol_for_series_slug


def test_tv_symbol_for_series_slug_lane_aware():
    assert tv_symbol_for_series_slug("btc-up-or-down-hourly") == "BTCUSDT"
    assert tv_symbol_for_series_slug("eth-up-or-down-hourly") == "ETHUSDT"
    assert tv_symbol_for_series_slug("btc-up-or-down-15m") == "BTCUSD"
    assert tv_symbol_for_series_slug("eth-up-or-down-15m") == "ETHUSD"


def test_tv_intake_tracks_only_active_mtf(tmp_path):
    from engine.pulse.tradingview import TradingViewIntake

    intake = TradingViewIntake(
        data_dir=str(tmp_path),
        secret="s",
        allowed_symbols=frozenset({"BTCUSD"}),
        mtf_timeframes=("5", "15", "30", "45"),
        drop_timeframes=frozenset({"60"}),
    )
    payload = {
        "secret": "s",
        "bot_name": "hermes",
        "symbol": "BTCUSD",
        "timeframe": "60",
        "direction": "UP",
        "strength": 0.8,
        "event_id": "legacy-60-bot1",
    }
    code, body = intake.ingest(json.dumps(payload).encode())
    assert code == 200 and body.get("accepted")
    assert ("BTCUSD", "60") not in intake.latest_by_tf

    payload["timeframe"] = "15"
    payload["event_id"] = "active-15-bot1"
    intake.ingest(json.dumps(payload).encode())
    assert ("BTCUSD", "15") in intake.latest_by_tf

    payload["timeframe"] = "45"
    payload["event_id"] = "active-45-bot1"
    code45, body45 = intake.ingest(json.dumps(payload).encode())
    assert code45 == 200 and body45.get("accepted")
    assert ("BTCUSD", "45") in intake.latest_by_tf

    payload["timeframe"] = "55"
    payload["event_id"] = "retired-55-bot1"
    code55, body55 = intake.ingest(json.dumps(payload).encode())
    assert code55 == 200 and body55.get("ignored") is True
    assert ("BTCUSD", "55") not in intake.latest_by_tf


def test_mtf_confirmation_ladder_order(tmp_path):
    from engine.pulse.tradingview import TradingViewIntake

    intake = TradingViewIntake(
        data_dir=str(tmp_path),
        secret="s",
        allowed_symbols=frozenset({"ETHUSD"}),
        mtf_timeframes=("5", "15", "30"),
        drop_timeframes=frozenset({"60"}),
    )
    now = 1_000_000.0
    for tf, direction in (("5", "UP"), ("15", "UP"), ("30", "DOWN")):
        intake.ingest(json.dumps({
            "secret": "s",
            "bot_name": "hermes",
            "symbol": "ETHUSD",
            "timeframe": tf,
            "direction": direction,
            "strength": 0.8,
            "event_id": "eth-%s" % tf,
        }).encode())
    mtf = intake.mtf_confirmation(symbol="ETHUSD", now=now + 5)
    assert mtf["mtf_timeframes"] == ["5", "15", "30"]
    assert mtf["fast_pair"] == ["5", "15"]
    assert mtf["confirm"] == "confirmed_up"
    assert mtf["confirm_3tf"] == "conflict_3tf"
    assert list(mtf["trend_by_tf"].keys()) == ["5", "15", "30"]
