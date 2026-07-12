"""Cross-timeframe (5m + 10m + 15m) TradingView confirmation — chart TFs must not overwrite each other.
OBSERVE-ONLY. Proves: fast-pair agreement yields confirmed_up/down; disagreement -> conflict;
only-one-fresh -> single_tf; and confirmation flows into the feature + grading buckets."""

from __future__ import annotations

import json

from engine.pulse.tradingview import TradingViewIntake, TradingViewEdge


def _intake(tmp_path, *, mtf_timeframes=("5", "10", "15")):
    # These tests assert the legacy 5/10/15 chart timeframes. Bot-1's live default is 2/3/4
    # (PULSE_TV_MTF_TIMEFRAMES), so the test fixture pins the timeframes it exercises rather than
    # relying on the production default.
    return TradingViewIntake(secret="s3cr3t", bot_name="hermes",
                             allowed_symbols=("BTCUSD",), data_dir=str(tmp_path),
                             mtf_timeframes=mtf_timeframes)


def _send(intake, *, direction, tf, now, bar_time=None):
    payload = {"secret": "s3cr3t", "bot_name": "hermes", "symbol": "BTCUSD",
               "direction": direction, "timeframe": tf,
               "bar_time": bar_time if bar_time is not None else now,
               "event_id": "BTCUSD-%s-%s-%s" % (tf, int(now * 1000), direction)}
    return intake.ingest(json.dumps(payload).encode(), now=now)


def test_4m_5m_confirmation_states(tmp_path):
    ik = _intake(tmp_path, mtf_timeframes=("4", "5"))
    t = 1_000_000.0
    _send(ik, direction="DOWN", tf="5", now=t)
    _send(ik, direction="DOWN", tf="4", now=t + 30)
    c = ik.mtf_confirmation(symbol="BTCUSD", now=t + 31)
    assert c["confirm"] == "confirmed_down" and c["direction"] == "DOWN"
    assert c["tf_4m_dir"] == "DOWN" and c["tf_5m_dir"] == "DOWN"
    _send(ik, direction="UP", tf="4", now=t + 60)
    c2 = ik.mtf_confirmation(symbol="BTCUSD", now=t + 61)
    assert c2["confirm"] == "conflict" and c2["direction"] is None
    _send(ik, direction="UP", tf="4", now=t + ik.confirm_window_s + 40)
    c3 = ik.mtf_confirmation(symbol="BTCUSD", now=t + ik.confirm_window_s + 41)
    assert c3["confirm"] == "single_tf" and c3["tf_5m_dir"] is None and c3["tf_4m_dir"] == "UP"


def test_all_tfs_stored_separately_not_overriding(tmp_path):
    ik = _intake(tmp_path)
    t = 6_000_000.0
    for tf, direction in (("5", "UP"), ("10", "UP"), ("15", "DOWN")):
        _send(ik, direction=direction, tf=tf, now=t + int(tf))
    rep = ik.report()
    by_tf = rep["tradingview_latest_by_timeframe"]
    assert by_tf["BTCUSD@5"]["direction"] == "UP"
    assert by_tf["BTCUSD@10"]["direction"] == "UP"
    assert by_tf["BTCUSD@15"]["direction"] == "DOWN"
    mtf = ik.mtf_confirmation(symbol="BTCUSD", now=t + 20)
    assert mtf["tf_5m_dir"] == "UP"
    assert mtf["tf_10m_dir"] == "UP"
    assert mtf["tf_15m_dir"] == "DOWN"


def test_13m_timeframe_normalized_from_suffix(tmp_path):
    ik = _intake(tmp_path, mtf_timeframes=("5", "10", "15", "13"))
    t = 6_100_000.0
    payload = {"secret": "s3cr3t", "bot_name": "hermes", "symbol": "BTCUSD",
               "direction": "UP", "timeframe": "13m", "bar_time": t,
               "event_id": "BTCUSD-13m-%d" % int(t * 1000)}
    code, body = ik.ingest(json.dumps(payload).encode(), now=t)
    assert code == 200 and body.get("accepted")
    assert "BTCUSD@13" in ik.report()["tradingview_latest_by_timeframe"]


def test_3tf_trend_alignment(tmp_path):
    ik = _intake(tmp_path)
    t = 6_200_000.0
    for tf in ("5", "10", "15"):
        _send(ik, direction="UP", tf=tf, now=t + int(tf))
    mtf = ik.mtf_confirmation(symbol="BTCUSD", now=t + 20)
    assert mtf["confirm_3tf"] == "confirmed_up_3tf"
    assert mtf["confirm_mtf"] == "confirmed_up_mtf"
    assert mtf["direction_3tf"] == "UP"
    assert mtf["trend_fresh_count"] == 3
    feat = ik.latest_feature(now=t + 20, symbol="BTCUSD")
    assert feat["tf_confirm_3tf"] == "confirmed_up_3tf"
    assert feat["trend_by_tf"] == {"5": "UP", "10": "UP", "15": "UP"}


def test_15m_aligns_with_5m_10m(tmp_path):
    ik = _intake(tmp_path)
    t = 5_000_000.0
    _send(ik, direction="DOWN", tf="5", now=t)
    _send(ik, direction="DOWN", tf="10", now=t + 10)
    _send(ik, direction="DOWN", tf="15", now=t + 20)
    mtf = ik.mtf_confirmation(symbol="BTCUSD", now=t + 30)
    assert mtf["tf_15m_dir"] == "DOWN"
    assert mtf["confirm_3tf"] == "confirmed_down_3tf"
    feat = ik.latest_feature(now=t + 30, symbol="BTCUSD")
    assert feat["tf_15m_dir"] == "DOWN"
    assert feat["tf_confirm_3tf"] == "confirmed_down_3tf"


def test_confirmation_flows_into_feature_and_grades(tmp_path):
    ik = _intake(tmp_path)
    t = 2_000_000.0
    _send(ik, direction="DOWN", tf="5", now=t)
    _send(ik, direction="DOWN", tf="10", now=t + 10)
    feat = ik.latest_feature(now=t + 11, symbol="BTCUSD")
    assert feat["tf_confirm"] == "confirmed_down" and feat["tf_confirm_direction"] == "DOWN"
    rep = ik.report()
    assert "tradingview_mtf_confirmation" in rep
    assert "BTCUSD@5" in rep["tradingview_latest_by_timeframe"]
    assert "BTCUSD@10" in rep["tradingview_latest_by_timeframe"]
    edge = TradingViewEdge()
    edge.record(tv=feat, traded_side="down", outcome_up=False, won=True, pnl=4.0)
    er = edge.report()
    assert "by_tf_confirm" in er and "confirmed_down" in er["by_tf_confirm"]
    assert er["by_tf_confirm"]["confirmed_down"]["n"] == 1


def test_index_mtf_via_feature_symbol(tmp_path):
    """Operator feeds INDEX:BTCUSD on 5m+10m charts — MTF resolves under BTCUSD."""
    ik = TradingViewIntake(secret="s3cr3t", bot_name="hermes",
                           allowed_symbols=("BTCUSD", "INDEX:BTCUSD"), data_dir=str(tmp_path),
                           feature_symbol="BTCUSD", mtf_timeframes=("5", "10", "15"))
    t = 4_000_000.0
    for tf, ts in (("5", t), ("10", t + 10)):
        payload = {"secret": "s3cr3t", "bot_name": "hermes", "symbol": "INDEX:BTCUSD",
                   "direction": "UP", "timeframe": tf,
                   "bar_time": ts, "event_id": "BTCUSD-%s-%d-UP" % (tf, int(ts * 1000))}
        ik.ingest(json.dumps(payload).encode(), now=ts)
    c = ik.mtf_confirmation(symbol="btc/usd", now=t + 11)
    assert c["confirm"] == "confirmed_up" and c["symbol"] == "BTCUSD"
    feat = ik.latest_feature(now=t + 11, symbol="btc/usd")
    assert feat["tf_confirm"] == "confirmed_up"


def test_confirmation_survives_restart(tmp_path):
    # Use current (non-legacy) timeframes: _canonicalize_storage deliberately purges the legacy
    # 5/10/15 TFs on reload (the bot migrated to 2/3/4), so a restart test must use live TFs.
    ik = _intake(tmp_path, mtf_timeframes=("2", "3", "4"))
    t = 3_000_000.0
    _send(ik, direction="UP", tf="2", now=t)
    _send(ik, direction="UP", tf="3", now=t + 5)
    ik2 = _intake(tmp_path, mtf_timeframes=("2", "3", "4"))
    c = ik2.mtf_confirmation(symbol="BTCUSD", now=t + 6)
    assert c["confirm"] == "confirmed_up" and c["direction"] == "UP"