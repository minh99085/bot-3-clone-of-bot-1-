"""Tests for Polymarket Asset Triage skill (Discovery Lane)."""

from __future__ import annotations

from engine.pulse.loop_architecture.asset_triage import (
    PROCEED_10X,
    PROCEED_SWEEP,
    AssetTriageSkill,
    TriageConfig,
    TriageReject,
)
from engine.pulse.markets import OrderBook, PulseWindow


def _window(ask: float = 0.50, depth: float = 10000.0) -> PulseWindow:
    book = OrderBook(
        best_bid=ask - 0.02,
        best_ask=ask,
        ask_depth_usd=depth,
        bid_depth_usd=depth,
        asks=[(ask, depth / ask), (ask + 0.01, depth)],
        bids=[(ask - 0.02, depth)],
    )
    return PulseWindow(
        event_id="evt-1",
        market_id="m1",
        slug="btc-up-or-down-hourly-test",
        title="BTC hourly",
        open_ts=1_000_000.0,
        close_ts=1_003_600.0,
        up_token_id="up-tok",
        down_token_id="dn-tok",
        series_slug="btc-up-or-down-hourly",
        up_book=book,
        down_book=book,
    )


def test_proceed_sweep_with_aligned_tv():
    skill = AssetTriageSkill(cfg=TriageConfig(trend_source="tv"))
    w = _window(0.50)
    tv = {"timeframe": "15", "direction": "UP", "strength": 0.6, "age_s": 10.0}
    v = skill.evaluate(window=w, side="up", ask_price=0.50, now=1_000_100.0,
                       tv_feature=tv, symbol="BTCUSD")
    assert v.status == PROCEED_SWEEP
    assert v.proceed is True
    assert v.token_id == "up-tok"


def test_proceed_sweep_with_5m_tv():
    skill = AssetTriageSkill(cfg=TriageConfig(trend_source="tv"))
    w = _window(0.50)
    tv = {"timeframe": "5", "direction": "UP", "strength": 0.6, "age_s": 5.0}
    v = skill.evaluate(window=w, side="up", ask_price=0.50, now=1_000_100.0,
                       tv_feature=tv, symbol="BTCUSD")
    assert v.status == PROCEED_SWEEP


def test_reject_tv_misaligned():
    skill = AssetTriageSkill(cfg=TriageConfig(trend_source="tv"))
    w = _window(0.50)
    tv = {"timeframe": "30", "direction": "DOWN", "strength": 0.6, "age_s": 5.0}
    v = skill.evaluate(window=w, side="up", ask_price=0.50, now=1_000_100.0,
                       tv_feature=tv, symbol="BTCUSD")
    assert v.status == TriageReject.TV_MISALIGNED.value


def test_proceed_10x_tail_with_breakthrough():
    skill = AssetTriageSkill(cfg=TriageConfig(trend_source="tv"))
    w = _window(0.08)
    tv = {"timeframe": "60", "direction": "UP", "strength": 0.7, "age_s": 5.0}
    v = skill.evaluate(window=w, side="up", ask_price=0.08, now=1_000_100.0,
                       tv_feature=tv, symbol="BTCUSD")
    assert v.status == PROCEED_10X


def test_reject_wrong_timeframe():
    skill = AssetTriageSkill(cfg=TriageConfig(
        trend_source="tv",
        tv_timeframes=("5", "15", "30", "60", "240", "1440"),
    ))
    w = _window(0.50)
    tv = {"timeframe": "10", "direction": "UP", "strength": 0.6, "age_s": 5.0}
    v = skill.evaluate(window=w, side="up", ask_price=0.50, now=1_000_100.0,
                       tv_feature=tv, symbol="BTCUSD")
    assert v.status == TriageReject.WRONG_TIMEFRAME.value


def test_memory_record_triage(tmp_path):
    from engine.pulse.loop_architecture.memory import LoopMemory
    mem = LoopMemory(tmp_path)
    mem.record_triage(
        token_id="up-tok-123",
        time_boundary="1003600",
        status=PROCEED_SWEEP,
        symbol="BTCUSD",
        timeframe="15",
        side="up",
    )
    mem.save()
    text = mem.path.read_text(encoding="utf-8")
    assert "up-tok-123" in text
    assert PROCEED_SWEEP in text
    assert "Recent triage" in text


def test_eth_window_uses_eth_triage_config(monkeypatch):
    monkeypatch.setenv("PULSE_TRIAGE_ETH_MIN_DEPTH_USD", "25")
    monkeypatch.setenv("PULSE_TRIAGE_BTC_MIN_DEPTH_USD", "50")
    skill = AssetTriageSkill()
    eth_cfg = skill.cfg_for("ETHUSD")
    btc_cfg = skill.cfg_for("BTCUSD")
    assert eth_cfg.min_depth_usd == 25.0
    assert btc_cfg.min_depth_usd == 50.0


def test_eth_series_slug_resolves_eth_asset_config(monkeypatch):
    monkeypatch.setenv("PULSE_TRIAGE_ETH_SWEET_MIN", "0.45")
    skill = AssetTriageSkill()
    w = _window(0.50)
    w.series_slug = "eth-up-or-down-hourly"
    cfg = skill.cfg_for(None, w)
    assert cfg.sweet_min == 0.45


def test_report_includes_per_asset_thresholds():
    skill = AssetTriageSkill(cfg=TriageConfig(trend_source="tv"))
    rep = skill.report()
    assert "btc" in rep["thresholds_by_asset"]
    assert "eth" in rep["thresholds_by_asset"]
    assert rep["thresholds"]["sweet_min"] == 0.47
