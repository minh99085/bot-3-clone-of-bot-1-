"""1h directional: fresh *_STRONG TV contrarian veto."""

from __future__ import annotations

from engine.pulse.engine import PulseEngine, PulseConfig
from engine.pulse.tradingview import TradingViewIntake, TradingViewSignalEvent


def _engine(tmp_path):
    return PulseEngine(PulseConfig(
        data_dir=str(tmp_path),
        tv_strong_fade_enabled=True,
        hourly_entry_gate_enabled=False,
        selectivity_gate_enabled=False,
        min_seconds_since_open=0.0,
        sigma_trust_floor=0.0,
        min_vol_samples=2,
    ))


def test_tv_strong_fade_veto_blocks_up_on_up_strong(tmp_path):
    eng = _engine(tmp_path)
    ok, reason = eng._tv_strong_fade_veto_ok("up", {"signal_level": "UP_STRONG"})
    assert not ok and reason == "tv_strong_fade_veto_up"
    ok2, _ = eng._tv_strong_fade_veto_ok("down", {"signal_level": "UP_STRONG"})
    assert ok2


def test_tv_strong_fade_veto_blocks_down_on_down_strong(tmp_path):
    eng = _engine(tmp_path)
    ok, reason = eng._tv_strong_fade_veto_ok("down", {"signal_level": "DOWN_STRONG"})
    assert not ok and reason == "tv_strong_fade_veto_down"


def test_tv_strong_fade_passes_weak_and_missing(tmp_path):
    eng = _engine(tmp_path)
    assert eng._tv_strong_fade_veto_ok("up", {"signal_level": "UP_WEAK"})[0]
    assert eng._tv_strong_fade_veto_ok("up", None)[0]
    assert eng._tv_strong_fade_veto_ok("up", {"signal_level": "FLAT"})[0]


def test_tv_strong_fade_exempt_tier_snipe_default_on(tmp_path):
    eng = _engine(tmp_path)
    assert eng.cfg.tv_strong_fade_exempt_tier_snipe is True


def test_tv_strong_fade_exempt_tier_snipe_can_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_TV_STRONG_FADE_EXEMPT_TIER_SNIPE", "0")
    eng = PulseEngine(PulseConfig(
        data_dir=str(tmp_path),
        tv_strong_fade_exempt_tier_snipe=False,
    ))
    assert eng.cfg.tv_strong_fade_exempt_tier_snipe is False


def test_latest_feature_for_symbol_uses_per_symbol_latest():
    intake = TradingViewIntake(
        secret="s", allowed_symbols=frozenset({"BTCUSD", "ETHUSD"}),
        feature_symbol="BTCUSD")
    now = 1000.0
    intake.latest_by_symbol["BTCUSD"] = TradingViewSignalEvent(
        event_id="a", bot_name="bot1", symbol="BTCUSD", timeframe="15",
        bar_time=now - 10.0, received_at=now - 10.0, direction="UP", strength=0.9,
        indicator_name="test", raw_payload_hash="h1", signal_level="UP_STRONG")
    intake.latest_by_symbol["ETHUSD"] = TradingViewSignalEvent(
        event_id="b", bot_name="bot1", symbol="ETHUSD", timeframe="15",
        bar_time=now - 5.0, received_at=now - 5.0, direction="DOWN", strength=0.8,
        indicator_name="test", raw_payload_hash="h2", signal_level="DOWN_WEAK")
    btc = intake.latest_feature_for_symbol("BTCUSD", now=now)
    eth = intake.latest_feature_for_symbol("ETHUSD", now=now)
    assert btc["signal_level"] == "UP_STRONG"
    assert eth["signal_level"] == "DOWN_WEAK"
