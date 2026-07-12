"""Grok decision bundle v1.3 helpers — per-market stats, gate funnel, 5-TF TV trend."""

from __future__ import annotations

from engine.pulse.grok_bundle import (classify_grok_compute_tier, compact_bundle_for_light_tier,
                                      compact_tv_learning, gate_funnel_top, grok_task_for_window,
                                      order_bundle_for_grok, serialize_bundle_for_grok,
                                      summarize_alert_trend, tv_alert_history_snapshot,
                                      tv_trend_snapshot)


def test_gate_funnel_top_sorted():
    funnel = gate_funnel_top({
        "context_gate": 100,
        "down_bias_gate": 200,
        "grok_decider": 5,
        "execution_gate": 50,
    }, top_n=3)
    assert funnel["total_rejected"] == 355
    assert funnel["top_blockers"][0] == {"stage": "down_bias_gate", "count": 200}
    assert funnel["top_blockers"][1]["stage"] == "context_gate"


def test_tv_trend_snapshot_trend_ladder():
    mtf = {
        "mtf_timeframes": ["15", "30", "45", "55"],
        "mtf_count": 4,
        "tf_15m_dir": "UP",
        "tf_30m_dir": "UP",
        "tf_45m_dir": "UP",
        "tf_55m_dir": "UP",
        "tf_15m_age_s": 60.0,
        "tf_30m_age_s": 120.0,
        "tf_45m_age_s": 180.0,
        "tf_55m_age_s": 240.0,
        "confirm_4tf": "confirmed_up_4tf",
        "confirm_mtf": "confirmed_up_mtf",
        "trend_fresh_count": 4,
        "trend_by_tf": {"15": "UP", "30": "UP", "45": "UP", "55": "UP"},
    }
    by_tf = {
        "ETHUSD@15": {"direction": "UP", "strength": 0.7, "signal_level": "UP_STRONG"},
        "ETHUSD@30": {"direction": "UP", "strength": 0.75},
        "ETHUSD@45": {"direction": "UP", "strength": 0.8},
        "ETHUSD@55": {"direction": "UP", "strength": 0.85},
    }
    snap = tv_trend_snapshot(mtf=mtf, latest_by_timeframe=by_tf, feature_symbol="ETHUSD")
    assert snap["feature_symbol"] == "ETHUSD"
    assert [r["label"] for r in snap["trend_ladder"]] == ["15m", "30m", "45m", "55m"]
    assert snap["trend_builds"] == "confirmed_up"
    assert snap["charts"]["55m"]["strength"] == 0.85


def test_grok_task_1h_tv_ladder():
    task = grok_task_for_window(series_label="1h", window_seconds=3600, ttc_s=1800.0)
    assert task["horizon"] == "1h_chainlink_window"
    assert "BTCUSDT" in task["tv_role"]
    assert "tradingview_alert_interpretation" in task["tv_role"]
    assert task["decision_priority"][0] == "1_tradingview_alert_interpretation"
    assert "1a_tv_5m_bar_close_short_path_pattern" in task["decision_priority"]


def test_tv_trend_snapshot_all_five_charts():
    mtf = {
        "mtf_timeframes": ["4", "5", "10", "13", "15"],
        "mtf_count": 5,
        "tf_4m_dir": "DOWN",
        "tf_5m_dir": "UP",
        "tf_10m_dir": "UP",
        "tf_13m_dir": "UP",
        "tf_15m_dir": "UP",
        "tf_4m_age_s": 45.0,
        "tf_5m_age_s": 120.0,
        "tf_10m_age_s": 200.0,
        "tf_13m_age_s": 250.0,
        "tf_15m_age_s": 300.0,
        "confirm_5tf": "partial_up_5tf",
        "confirm_mtf": "partial_up_mtf",
        "direction_5tf": "UP",
        "direction_mtf": "UP",
        "trend_fresh_count": 5,
        "trend_by_tf": {"4": "DOWN", "5": "UP", "10": "UP", "13": "UP", "15": "UP"},
    }
    by_tf = {
        "BTCUSD@4": {"direction": "DOWN", "strength": 0.61},
        "BTCUSD@5": {"direction": "UP", "strength": 0.75},
        "BTCUSD@10": {"direction": "UP", "strength": 0.79},
        "BTCUSD@13": {"direction": "UP", "strength": 0.80},
        "BTCUSD@15": {"direction": "UP", "strength": 0.82},
    }
    snap = tv_trend_snapshot(mtf=mtf, latest_by_timeframe=by_tf, feature_symbol="BTCUSD")
    assert snap["confirm_5tf"] == "partial_up_5tf"
    assert snap["confirm_mtf"] == "partial_up_mtf"
    assert snap["direction_5tf"] == "UP"
    assert snap["charts"]["10m"]["direction"] == "UP"
    assert snap["charts"]["10m"]["strength"] == 0.79
    assert snap["charts"]["10m"]["fresh"] is True
    assert snap["charts"]["4m"]["age_s"] == 45.0


def test_tv_trend_stale_fallback():
    mtf = {"mtf_timeframes": ["4", "5", "10", "13", "15"], "mtf_count": 5,
           "tf_5m_dir": None, "tf_10m_dir": "UP", "tf_10m_age_s": 90.0,
           "confirm_5tf": "single_tf", "confirm_mtf": "single_tf",
           "direction_5tf": "UP", "direction_mtf": "UP", "trend_fresh_count": 1}
    by_tf = {"BTCUSD@5": {"direction": "DOWN", "strength": 0.55}}
    snap = tv_trend_snapshot(mtf=mtf, latest_by_timeframe=by_tf)
    assert snap["charts"]["5m"]["direction"] == "DOWN"
    assert snap["charts"]["5m"]["fresh"] is False
    assert snap["charts"]["5m"]["stale_stored_dir"] == "DOWN"


def test_tv_trend_includes_signal_level():
    mtf = {"mtf_timeframes": ["2", "3", "4"], "mtf_count": 3,
           "tf_2m_dir": "UP", "tf_2m_age_s": 10.0, "confirm_mtf": "confirmed_up_mtf",
           "trend_fresh_count": 1}
    by_tf = {"BTCUSD@2": {"direction": "UP", "strength": 0.8, "signal_level": "UP_STRONG"}}
    snap = tv_trend_snapshot(mtf=mtf, latest_by_timeframe=by_tf)
    assert snap["charts"]["2m"]["signal_level"] == "UP_STRONG"


def test_grok_task_15m_entry_band():
    # Sweet band for 15m lane: TTC 120–420s (Hermes BarClose + lane learner).
    task = grok_task_for_window(series_label="15m", window_seconds=900, ttc_s=300.0)
    assert task["in_entry_band"] is True
    assert task["horizon"] == "15m_chainlink_window"
    assert task["entry_band_ttc_s"] == [120, 420]
    out = grok_task_for_window(series_label="15m", window_seconds=900, ttc_s=500.0)
    assert out["in_entry_band"] is False


def test_bundle_priority_ordering():
    b = order_bundle_for_grok({
        "lessons": [1], "tradingview_trend": {"x": 1}, "cex_lead_mispricing": {"d": 0.1},
    })
    keys = list(b.keys())
    assert keys.index("tradingview_trend") < keys.index("lessons")
    assert keys.index("cex_lead_mispricing") < keys.index("lessons")


def test_compact_tv_learning():
    out = compact_tv_learning({
        "settled_with_signal": 40,
        "best_signal_levels": [{"signal_level": "UP_STRONG", "win_rate": 0.7}],
        "by_signal_level": {"UP_STRONG": {"n": 10}, "FLAT": {"n": 5}},
    })
    assert out["best_signal_levels"][0]["signal_level"] == "UP_STRONG"
    assert "UP_STRONG" in out["by_signal_level"]


def test_serialize_bundle_truncates_tail():
    big = {"tradingview_trend": {"a": 1}, "lessons": ["x" * 5000]}
    ordered = order_bundle_for_grok(big)
    raw = serialize_bundle_for_grok(ordered, max_chars=200)
    assert "tradingview_trend" in raw


def test_classify_tier_light_vs_full():
    base = {
        "grok_task": {"in_entry_band": False},
        "cex_lead_mispricing": {"divergence": 0.01, "tv_confirms": False, "confirmed": False},
        "tradingview_trend": {"confirm_mtf": "none", "fresh_tf_count": 0},
    }
    assert classify_grok_compute_tier(base) == "light"
    full = dict(base)
    full["cex_lead_mispricing"] = {"divergence": 0.04, "tv_confirms": True, "confirmed": True}
    full["tradingview_trend"] = {"confirm_mtf": "confirmed_down_mtf", "fresh_tf_count": 3}
    assert classify_grok_compute_tier(full) == "full"


def test_classify_tier_deep_on_15m_entry_band():
    bundle = {
        "grok_task": {"in_entry_band": True},
        "cex_lead_mispricing": {"divergence": 0.05, "tv_confirms": True, "confirmed": True},
        "tradingview_trend": {"confirm_mtf": "confirmed_down_mtf", "fresh_tf_count": 3},
    }
    assert classify_grok_compute_tier(bundle) == "deep"


def test_compact_light_bundle_drops_history():
    full = {
        "schema_version": "grok_decision_bundle/1.4",
        "grok_compute_tier": "light",
        "trade_decision_history": [{"x": 1}],
        "timing": {"seconds_to_close": 500},
        "cex_lead_mispricing": {"divergence": 0.01},
        "tradingview_trend": {"confirm_mtf": "none", "charts": {"2m": {"direction": "FLAT"}}},
    }
    lite = compact_bundle_for_light_tier(full)
    assert "trade_decision_history" not in lite
    assert lite["grok_compute_tier"] == "light"
    assert "2m" in lite["tradingview_trend"]["charts"]


def test_summarize_alert_trend_uptrend_streak():
    alerts = [{"direction": "UP", "price": 100.0},
              {"direction": "UP", "price": 101.0},
              {"direction": "UP", "price": 102.5}]
    trend = summarize_alert_trend(alerts)
    assert trend["pattern"] == "uptrend"
    assert trend["current_streak_dir"] == "UP"
    assert trend["current_streak_len"] == 3
    assert trend["price_delta_pct"] == 2.5


def test_summarize_alert_trend_choppy():
    alerts = [{"direction": "UP"}, {"direction": "DOWN"}, {"direction": "UP"}]
    trend = summarize_alert_trend(alerts)
    assert trend["pattern"] == "choppy"


def test_tv_alert_history_snapshot_per_symbol():
    history = {
        "per_symbol_limit": 10,
        "focus_symbol": "ETHUSD",
        "by_symbol": {
            "BTCUSD": [
                {"event_id": "b1", "direction": "UP", "price": 60000},
                {"event_id": "b2", "direction": "UP", "price": 60100},
                {"event_id": "b3", "direction": "UP", "price": 60200},
            ],
            "ETHUSD": [
                {"event_id": "e1", "direction": "DOWN", "price": 3000},
                {"event_id": "e2", "direction": "DOWN", "price": 2980},
                {"event_id": "e3", "direction": "DOWN", "price": 2960},
            ],
        },
    }
    snap = tv_alert_history_snapshot(history=history, focus_symbol="ETHUSD", per_symbol_limit=10)
    assert snap["focus_symbol"] == "ETHUSD"
    assert snap["focus"]["trend"]["pattern"] == "downtrend"
    assert len(snap["by_symbol"]["BTCUSD"]["alerts"]) == 3
    assert snap["by_symbol"]["BTCUSD"]["trend"]["pattern"] == "uptrend"


def test_grok_task_mentions_alert_history():
    task = grok_task_for_window(series_label="1h", window_seconds=3600, ttc_s=1800.0)
    assert "tradingview_alert_interpretation" in task["tv_role"]
    assert "1g_tradingview_alert_history_trend_pattern" in task["decision_priority"]


def test_bundle_priority_includes_alert_history():
    b = order_bundle_for_grok({
        "tradingview_alert_interpretation": {"composite_lean": "up"},
        "tradingview_alert_history": {"focus": {}},
        "tradingview_signal": {"x": 1},
        "lessons": [1],
    })
    keys = list(b.keys())
    assert keys.index("tradingview_alert_interpretation") < keys.index("tradingview_signal")
    assert keys.index("tradingview_alert_history") < keys.index("tradingview_signal")
    assert keys.index("tradingview_alert_history") < keys.index("lessons")