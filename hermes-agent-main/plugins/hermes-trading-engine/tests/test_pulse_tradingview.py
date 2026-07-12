"""TradingView indicator-alert intake — OBSERVE-ONLY (acceptance criteria #1-#9).

Covers: valid alert, bad/missing secret, duplicate alert, stale timestamp, bad direction,
unsupported symbol, wrong bot, invalid JSON, dedupe persistence, the live HTTP listener, the
report fields, and PROOF that a TradingView signal cannot bypass the execution gate or force a
paper trade.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error

from engine.pulse.tradingview import (TradingViewIntake, normalize_direction, normalize_symbol,
                                       BAD_SECRET, MISSING_SECRET, WRONG_BOT, UNSUPPORTED_SYMBOL,
                                       STALE_TIMESTAMP, MALFORMED_DIRECTION, DUPLICATE_EVENT_ID,
                                       WRONG_EVENT_SUFFIX, INVALID_JSON)
from engine.pulse.tradingview import TradingViewEdge, RSITrendModel
from engine.pulse.webhook import WebhookServer
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig

SECRET = "s3cr3t-token"


def _intake(tmp_path=None, **kw):
    return TradingViewIntake(secret=SECRET, allowed_symbols=["BTCUSD", "INDEX:BTCUSD"],
                             bot_name="hermes", max_age_s=90.0,
                             data_dir=(str(tmp_path) if tmp_path else None), **kw)


def _alert(**over):
    base = {"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD", "timeframe": "5",
            "direction": "UP", "strength": 0.8, "indicator_name": "supertrend",
            "event_id": "evt-1", "bar_time": None}
    base.update(over)
    return json.dumps(base).encode("utf-8")


# ------------------------------- normalization --------------------------------------------- #
def test_direction_normalization():
    assert normalize_direction("up") == "UP" and normalize_direction("LONG") == "UP"
    assert normalize_direction("sell") == "DOWN" and normalize_direction("Bearish") == "DOWN"
    assert normalize_direction("flat") == "FLAT" and normalize_direction("neutral") == "FLAT"
    assert normalize_direction("sideways-ish") is None and normalize_direction(None) is None


# ------------------------------- valid alert (#3,#4) --------------------------------------- #
def test_valid_alert_normalized_event():
    intake = _intake()
    now = 1_000_000.0
    code, body = intake.ingest(_alert(bar_time=now - 5), now=now)
    assert code == 200 and body["accepted"] is True and body["observe_only"] is True
    ev = intake.latest
    assert ev.source == "tradingview" and ev.observe_only is True
    assert ev.event_id == "evt-1" and ev.bot_name == "hermes" and ev.symbol == "BTCUSD"
    assert ev.timeframe == "5" and ev.direction == "UP" and ev.strength == 0.8
    assert ev.indicator_name == "supertrend" and len(ev.raw_payload_hash) == 64
    assert ev.bar_time == now - 5 and ev.received_at == now
    assert intake.valid == 1 and intake.rejected == 0 and intake.received == 1


def test_symbol_normalization_strips_exchange_prefix():
    assert normalize_symbol("INDEX:BTCUSD") == "BTCUSD"
    assert normalize_symbol("COINBASE:BTCUSD") == "BTCUSD"
    assert normalize_symbol("btcusd") == "BTCUSD" and normalize_symbol("BTC/USD") == "BTC/USD"


def test_btc_aliases_collapse_to_feature_symbol():
    """INDEX:BTCUSD + BTCUSD both stored under feature_symbol (default BTCUSD)."""
    intake = _intake(feature_symbol="BTCUSD")
    now = 1_000_000.0
    idx = json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "INDEX:BTCUSD",
                      "direction": "UP", "strength": 0.7, "indicator_name": "RSI Divergence",
                      "event_id": "idx-1"}).encode()
    spot = json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD",
                       "direction": "DOWN", "strength": 0.6, "indicator_name": "RSI Divergence",
                       "event_id": "usd-1"}).encode()
    assert intake.ingest(idx, now=now)[1]["accepted"] is True
    assert intake.ingest(spot, now=now + 1)[1]["accepted"] is True
    rep = intake.report()
    assert rep["tradingview_alerts_valid"] == 2
    assert rep["tradingview_valid_by_symbol"] == {"BTCUSD": 2}
    bysym = rep["tradingview_latest_by_symbol"]
    assert set(bysym) == {"BTCUSD"}
    assert bysym["BTCUSD"]["direction"] == "DOWN"
    assert intake.ingest(idx, now=now + 2)[1].get("duplicate") is True
    assert intake.report()["tradingview_valid_by_symbol"] == {"BTCUSD": 2}


def test_btcusdt_rejected_without_allowlist():
    intake = _intake()
    raw = json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BINANCE:BTCUSDT",
                      "direction": "UP", "event_id": "bn-1"}).encode()
    code, body = intake.ingest(raw, now=1_000_000.0)
    assert code == 400 and body["reason"] == UNSUPPORTED_SYMBOL


def test_btcusdt_accepted_when_allowlisted():
    """Above-strike lane stores BINANCE:BTCUSDT alerts under BTCUSDT (not BTCUSD)."""
    intake = TradingViewIntake(
        secret=SECRET, allowed_symbols=["BTCUSD", "BTCUSDT", "BINANCE:BTCUSDT"],
        bot_name="hermes", max_age_s=90.0)
    raw = json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BINANCE:BTCUSDT",
                      "timeframe": "15", "direction": "UP", "event_id": "bn-above-1"}).encode()
    code, body = intake.ingest(raw, now=1_000_000.0)
    assert code == 200 and body["accepted"] is True
    rep = intake.report()
    assert rep["tradingview_valid_by_symbol"] == {"BTCUSDT": 1}
    assert "BTCUSD" not in rep["tradingview_valid_by_symbol"]


def test_per_symbol_state_persists(tmp_path):
    intake = _intake(tmp_path, feature_symbol="BTCUSD")
    intake.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "INDEX:BTCUSD",
                              "direction": "UP", "event_id": "idx-x"}).encode(), now=1_000_000.0)
    intake.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD",
                              "direction": "DOWN", "event_id": "usd-x"}).encode(), now=1_000_001.0)
    restored = _intake(tmp_path, feature_symbol="BTCUSD")
    rep = restored.report()
    assert rep["tradingview_valid_by_symbol"] == {"BTCUSD": 2}
    assert set(rep["tradingview_latest_by_symbol"]) == {"BTCUSD"}


def test_legacy_storage_merged_on_load(tmp_path):
    """Legacy per-ticker keys collapse into BTCUSD feature_symbol after restart."""
    state_path = tmp_path / "btc_pulse_tradingview.json"
    state_path.write_text(json.dumps({
        "received": 2, "valid": 2, "rejected": 0, "consumed": 0, "reject_reasons": {},
        "seen_ids": ["tv-test-1", "idx-1"],
        "latest": {"event_id": "idx-1", "bot_name": "hermes", "symbol": "BTCUSD",
                   "direction": "DOWN", "received_at": 2.0, "raw_payload_hash": "a" * 64},
        "latest_by_symbol": {
            "BTCUSD": {"event_id": "tv-test-1", "bot_name": "hermes", "symbol": "BTCUSD",
                       "direction": "UP", "received_at": 1.0, "raw_payload_hash": "b" * 64},
            "INDEX:BTCUSD": {"event_id": "idx-1", "bot_name": "hermes", "symbol": "BTCUSD",
                             "direction": "DOWN", "received_at": 2.0, "raw_payload_hash": "a" * 64},
        },
        "valid_by_symbol": {"BTCUSD": 1, "INDEX:BTCUSD": 1},
        "latest_by_tf": [],
    }), encoding="utf-8")
    intake = _intake(tmp_path, feature_symbol="BTCUSD")
    rep = intake.report()
    assert rep["tradingview_valid_by_symbol"] == {"BTCUSD": 2}
    assert set(rep["tradingview_latest_by_symbol"]) == {"BTCUSD"}
    assert rep["tradingview_latest_by_symbol"]["BTCUSD"]["direction"] == "DOWN"


def test_rsi_trend_canonicalize_merges_index(tmp_path):
    m = RSITrendModel()
    m.observe(symbol="INDEX:BTCUSD", direction="DOWN", ts=1.0)
    m.observe(symbol="BTCUSD", direction="UP", ts=2.0)
    m.canonicalize_storage("BTCUSD")
    assert set(m.hist) == {"BTCUSD"}
    assert len(m.hist["BTCUSD"]) == 2
    assert m.trend("BTCUSD")["last_direction"] == "UP"


def test_secret_via_header_only():
    intake = _intake()
    raw = json.dumps({"bot_name": "hermes", "symbol": "BTCUSD", "direction": "DOWN"}).encode()
    code, body = intake.ingest(raw, provided_header=SECRET, now=1_000_000.0)
    assert code == 200 and body["accepted"] is True and body["direction"] == "DOWN"


# ------------------------------- rejections (#3) ------------------------------------------- #
def test_bad_secret_rejected():
    intake = _intake()
    code, body = intake.ingest(_alert(secret="WRONG"), now=1_000_000.0)
    assert code == 401 and body["reason"] == BAD_SECRET and intake.valid == 0
    assert intake.reject_reasons[BAD_SECRET] == 1


def test_missing_secret_rejected():
    intake = _intake()
    raw = json.dumps({"bot_name": "hermes", "symbol": "BTCUSD", "direction": "UP"}).encode()
    code, body = intake.ingest(raw, now=1_000_000.0)
    assert code == 401 and body["reason"] == MISSING_SECRET


def test_wrong_bot_rejected():
    intake = _intake()
    code, body = intake.ingest(_alert(bot_name="other-bot"), now=1_000_000.0)
    assert code == 400 and body["reason"] == WRONG_BOT


def test_wrong_event_suffix_rejected_on_bot1():
    intake = _intake(expected_event_id_suffix="bot1")
    code, body = intake.ingest(
        _alert(event_id="BTCUSD-2-1782702000000-UP_STRONG-lite-2-bot2"), now=1_000_000.0)
    assert code == 400 and body["reason"] == WRONG_EVENT_SUFFIX
    code2, body2 = intake.ingest(
        _alert(event_id="BTCUSD-2-1782702000000-UP_STRONG-lite-2-bot1"), now=1_000_001.0)
    assert code2 == 200 and body2.get("accepted") is True


def test_unsupported_symbol_rejected():
    intake = _intake()
    code, body = intake.ingest(_alert(symbol="ETHUSD"), now=1_000_000.0)
    assert code == 400 and body["reason"] == UNSUPPORTED_SYMBOL


def test_active_tf_kept_and_unsupported_rejects_scrubbed(tmp_path):
    intake = _intake(tmp_path, mtf_timeframes=("2", "5", "15"))
    now = 1_000_000.0
    intake.ingest(_alert(timeframe="2", event_id="e2"), now=now)
    intake.ingest(_alert(timeframe="5", event_id="e5"), now=now + 1)
    intake.ingest(_alert(timeframe="15", event_id="e15"), now=now + 2)
    intake.reject_reasons[UNSUPPORTED_SYMBOL] = 17
    intake.rejected = 19
    intake._scrub_legacy_reject_stats()
    intake._canonicalize_storage()
    # 5m/15m are ACTIVE horizon-matched TFs now -> must be KEPT (not pruned as legacy)
    assert ("BTCUSD", "5") in intake.latest_by_tf
    assert ("BTCUSD", "15") in intake.latest_by_tf
    # unsupported-symbol reject stats still scrubbed
    assert UNSUPPORTED_SYMBOL not in intake.reject_reasons
    assert intake.rejected == 2


def test_bot_name_allow_list_accepts_multiple(tmp_path):
    intake = _intake(tmp_path, allowed_bot_names=["hermes", "grokbot5m"])
    now = 1_000_000.0
    code, body = intake.ingest(_alert(bot_name="grokbot5m", event_id="e-alt"), now=now)
    assert code == 200 and body.get("ok") is True          # allow-listed alt bot name accepted
    code2, body2 = intake.ingest(_alert(bot_name="hermes", event_id="e-h"), now=now + 1)
    assert code2 == 200 and body2.get("ok") is True         # default still accepted
    code3, body3 = intake.ingest(_alert(bot_name="someoneelse", event_id="e-x"), now=now + 2)
    assert code3 != 200 and body3.get("reason") == WRONG_BOT  # not on the list -> rejected


def test_drop_timeframes_not_tracked_but_still_accepted(tmp_path):
    intake = _intake(tmp_path, drop_timeframes=("2", "3", "4"), mtf_timeframes=("5", "15"))
    now = 1_000_000.0
    for i, tf in enumerate(("2", "4", "5", "15")):
        code, body = intake.ingest(_alert(timeframe=tf, event_id="e-%s" % tf), now=now + i)
        assert code == 200 and body.get("ok") is True   # accepted (observe-only)
    tfs = {k[1] for k in intake.latest_by_tf}
    # retired short TFs are NOT tracked per-TF; horizon TFs are
    assert "2" not in tfs and "4" not in tfs
    assert "5" in tfs and "15" in tfs
    # alerts still counted as valid even though 2/4 are not tracked per-TF
    assert intake.valid == 4


def test_3m_4m_tracked_when_active_not_dropped(tmp_path):
    from engine.pulse.tradingview import TradingViewIntake
    intake = TradingViewIntake(
        secret=SECRET,
        allowed_symbols=["BTCUSD", "INDEX:BTCUSD", "ETHUSD", "INDEX:ETHUSD"],
        bot_name="hermes",
        max_age_s=90.0,
        data_dir=str(tmp_path),
        drop_timeframes=("2", "10"),
        mtf_timeframes=("3", "4", "5", "15"),
    )
    now = 1_000_000.0
    for i, tf in enumerate(("3", "4", "5", "ETHUSD-3")):
        sym = "ETHUSD" if tf == "ETHUSD-3" else "BTCUSD"
        tf_key = "3" if tf == "ETHUSD-3" else tf
        code, body = intake.ingest(
            _alert(timeframe=tf_key, symbol=sym, event_id="e-%s-%s" % (sym, tf_key)),
            now=now + i,
        )
        assert code == 200 and body.get("ok") is True
    tfs = {k[1] for k in intake.latest_by_tf}
    assert "3" in tfs and "4" in tfs and "5" in tfs
    assert ("ETHUSD", "3") in intake.latest_by_tf


def test_retired_timeframes_purged_from_persisted_snapshot(tmp_path):
    intake = _intake(tmp_path, mtf_timeframes=("5", "15", "30", "45", "55"))
    now = 1_000_000.0
    for tf in ("5", "15", "45", "55"):
        intake.ingest(_alert(timeframe=tf, event_id="purge-%s" % tf), now=now + int(tf))
    assert intake.valid == 4
    assert ("BTCUSD", "55") in intake.latest_by_tf
    intake.latest_by_tf[("BTCUSD", "55")] = intake.latest_by_tf[("BTCUSD", "15")]
    intake._seen.append("LEGACY-55-1-UP_WEAK-rsi1h-bot1")
    intake._seen_set.update(intake._seen)
    intake._persist_locked()
    intake2 = _intake(tmp_path, mtf_timeframes=("5", "15", "30", "45", "55"))
    assert ("BTCUSD", "55") in intake2.latest_by_tf


def test_retired_timeframe_ingest_not_persisted(tmp_path):
    intake = _intake(tmp_path, drop_timeframes=("2",), mtf_timeframes=("5", "15", "30", "45"))
    now = 1_000_000.0
    code, body = intake.ingest(_alert(timeframe="2", event_id="dropped-2m"), now=now)
    assert code == 200 and body.get("accepted") is True
    assert intake.valid == 1
    assert ("BTCUSD", "2") not in intake.latest_by_tf


def test_drop_timeframes_stripped_from_persisted_snapshot(tmp_path):
    intake = _intake(tmp_path, drop_timeframes=("3",), mtf_timeframes=("3", "5"))
    now = 1_000_000.0
    intake.ingest(_alert(timeframe="5", event_id="k5"), now=now)
    # simulate a legacy persisted entry for a now-dropped TF, then canonicalize (load path)
    intake.latest_by_tf[("BTCUSD", "3")] = intake.latest_by_tf[("BTCUSD", "5")]
    intake._canonicalize_storage()
    tfs = {k[1] for k in intake.latest_by_tf}
    assert "3" not in tfs and "5" in tfs


def test_btc_exchange_prefix_symbols_accepted():
    intake = _intake()
    for sym in ("BITSTAMP:BTCUSD", "COINBASE:BTCUSD", "BTC/USD"):
        code, body = intake.ingest(_alert(symbol=sym, event_id="btc-%s" % sym), now=1_000_000.0)
        assert code == 200 and body.get("accepted") is True, (sym, body)


def test_stale_timestamp_rejected():
    intake = _intake()
    now = 1_000_000.0
    code, body = intake.ingest(_alert(bar_time=now - 600), now=now)   # 10 min old > 90s
    assert code == 400 and body["reason"] == STALE_TIMESTAMP
    # far-future also rejected
    code2, body2 = intake.ingest(_alert(event_id="evt-future", bar_time=now + 600), now=now)
    assert code2 == 400 and body2["reason"] == STALE_TIMESTAMP


def test_bad_direction_rejected():
    intake = _intake()
    code, body = intake.ingest(_alert(direction="diagonal"), now=1_000_000.0)
    assert code == 400 and body["reason"] == MALFORMED_DIRECTION


def test_invalid_json_rejected():
    intake = _intake()
    code, body = intake.ingest(b"not-json{{", now=1_000_000.0)
    assert code == 400 and body["reason"] == INVALID_JSON


# ------------------------------- dedupe (#5) ----------------------------------------------- #
def test_duplicate_event_rejected():
    intake = _intake()
    now = 1_000_000.0
    c1, b1 = intake.ingest(_alert(event_id="dup-1"), now=now)
    c2, b2 = intake.ingest(_alert(event_id="dup-1"), now=now + 1)
    assert b1["accepted"] is True
    assert b2.get("duplicate") is True and b2["reason"] == DUPLICATE_EVENT_ID
    assert intake.valid == 1 and intake.reject_reasons[DUPLICATE_EVENT_ID] == 1
    # only one pending candidate was produced
    assert len(intake.drain_pending()) == 1


def test_dedupe_persists_across_restart(tmp_path):
    intake = _intake(tmp_path)
    intake.ingest(_alert(event_id="persist-1"), now=1_000_000.0)
    # a fresh intake on the same data dir must remember the seen id
    intake2 = _intake(tmp_path)
    assert intake.valid == 1                       # the first intake accepted it
    assert intake2.latest is not None and intake2.latest.event_id == "persist-1"   # latest restored
    code, body = intake2.ingest(_alert(event_id="persist-1"), now=1_000_100.0)
    assert body.get("duplicate") is True and body["reason"] == DUPLICATE_EVENT_ID
    # restored counters carry the prior valid=1; the duplicate does NOT add a new valid candidate
    assert intake2.valid == 1 and len(intake2.drain_pending()) == 0
    assert intake2.reject_reasons[DUPLICATE_EVENT_ID] == 1


# ------------------------------- report fields (#8) ---------------------------------------- #
def test_report_fields_present():
    intake = _intake()
    intake.ingest(_alert(event_id="r-1"), now=1_000_000.0)
    intake.ingest(_alert(secret="WRONG", event_id="r-2"), now=1_000_000.0)
    rep = intake.report()
    for fld in ("tradingview_alerts_received", "tradingview_alerts_valid",
                "tradingview_alerts_rejected", "tradingview_reject_reasons",
                "tradingview_latest_signal", "tradingview_observe_only"):
        assert fld in rep, fld
    assert rep["tradingview_observe_only"] is True
    assert rep["tradingview_alerts_received"] == 2 and rep["tradingview_alerts_valid"] == 1
    assert rep["tradingview_alerts_rejected"] == 1
    assert rep["tradingview_latest_signal"]["event_id"] == "r-1"


# ------------------------------- live HTTP listener ---------------------------------------- #
def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=headers or {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_listener_end_to_end():
    intake = _intake()
    srv = WebhookServer(intake, host="127.0.0.1", port=0,
                        path="/webhooks/tradingview").start()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        assert srv.status()["bound_internal"] is True
        # valid POST
        code, body = _post(base + "/webhooks/tradingview", _alert(event_id="http-1"))
        assert code == 200 and body["accepted"] is True and body["observe_only"] is True
        # bad secret -> 401
        code, body = _post(base + "/webhooks/tradingview",
                           _alert(secret="nope", event_id="http-2"))
        assert code == 401 and body["reason"] == BAD_SECRET
        # wrong path -> 404
        code, _ = _post(base + "/nope", _alert(event_id="http-3"))
        assert code == 404
        # GET on the signal path is not a signal intake
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            assert json.loads(r.read())["observe_only"] is True
    finally:
        srv.stop()
    assert intake.valid == 1 and intake.reject_reasons.get(BAD_SECRET) == 1


# ============================= edge measurement ============================================ #
def test_edge_measurement_detects_predictive_signal():
    """A signal that is right 80% of the time should report a high signal_hit_rate + predictive
    verdict; alignment win-rate is tracked separately."""
    edge = TradingViewEdge()
    # 40 UP signals: outcome up 80% of the time; bot always traded 'up' (so aligned)
    for i in range(40):
        up = (i % 5 != 0)        # 32/40 correct
        edge.record(tv={"direction": "UP", "timeframe": "5", "symbol": "BTCUSD"},
                    traded_side="up", outcome_up=up, won=up, pnl=(2.0 if up else -5.0))
    # 20 trades with NO signal: coin-flip outcomes
    for i in range(20):
        up = (i % 2 == 0)
        edge.record(tv=None, traded_side="up", outcome_up=up, won=up, pnl=(2.0 if up else -5.0))
    rep = edge.report()
    assert rep["observe_only"] is True and rep["report_only"] is True
    assert rep["n_settled_with_signal"] == 40 and rep["n_settled_no_signal"] == 20
    assert rep["signal_evaluated_up_down"] == 40
    assert abs(rep["signal_hit_rate"] - 0.8) < 1e-6
    assert rep["verdict"] == "signal_predictive_edge"
    assert rep["by_direction"]["UP"]["signal_hit_rate"] == 0.8
    assert rep["by_symbol"]["BTCUSD"]["n"] == 40
    assert rep["by_alignment"]["aligned"]["n"] == 40
    assert rep["by_direction"]["none"]["n"] == 20      # no-signal trades bucketed separately


def test_edge_measurement_insufficient_evidence_and_inverse():
    edge = TradingViewEdge()
    for i in range(10):           # below MIN_EVIDENCE
        edge.record(tv={"direction": "UP", "timeframe": "5", "symbol": "BTCUSD"},
                    traded_side="up", outcome_up=True, won=True, pnl=2.0)
    assert edge.report()["verdict"] == "insufficient_evidence"
    # a consistently-wrong signal -> inverse-edge verdict (a fade)
    edge2 = TradingViewEdge()
    for i in range(40):
        down_signal_but_up = True
        edge2.record(tv={"direction": "DOWN", "timeframe": "5", "symbol": "BTCUSD"},
                     traded_side="down", outcome_up=down_signal_but_up, won=False, pnl=-5.0)
    r2 = edge2.report()
    assert r2["signal_hit_rate"] == 0.0 and r2["verdict"] == "signal_inverse_edge"


def test_edge_measurement_persists_round_trip():
    edge = TradingViewEdge()
    for i in range(5):
        edge.record(tv={"direction": "UP", "timeframe": "3", "symbol": "BTCUSD"},
                    traded_side="up", outcome_up=True, won=True, pnl=2.0)
    edge2 = TradingViewEdge()
    edge2.load_state(edge.to_state())
    assert edge2.report()["by_timeframe"]["3"]["n"] == 5
    assert edge2.signal_correct == 5 and edge2.n_total == 5


# ============================= RSI trend history model ===================================== #
def test_rsi_trend_classification_streak():
    m = RSITrendModel()
    for i, d in enumerate(["UP", "UP", "UP"]):
        m.observe(symbol="BTCUSD", direction=d, ts=1000 + i)
    t = m.trend("BTCUSD")
    assert t["last_direction"] == "UP" and t["streak"] == 3 and t["state"] == "up_streak3"
    m.observe(symbol="BTCUSD", direction="DOWN", ts=1100)
    assert m.trend("BTCUSD")["state"] == "down_streak1"


def test_rsi_predictor_learns_and_scores_leakage_free():
    """In trend state 'up_streak1' the next outcome is UP 90% of the time; after enough settled
    samples the model predicts UP for that state and scores its own (leakage-free) predictions."""
    m = RSITrendModel()
    ts = 1000.0
    hits = 0
    n = 0
    for i in range(60):
        # produce an 'up_streak1' state: a single UP after a DOWN
        m.observe(symbol="BTCUSD", direction="DOWN", ts=ts); ts += 1
        m.observe(symbol="BTCUSD", direction="UP", ts=ts); ts += 1
        state = m.trend("BTCUSD")["state"]
        pred = m.predict("BTCUSD")          # leakage-free: uses counts excluding this outcome
        outcome_up = (i % 10 != 0)          # UP 90% of the time
        if pred.get("prediction") in ("UP", "DOWN"):
            n += 1
            hits += int((pred["prediction"] == "UP") == outcome_up)
        m.score_and_update(symbol="BTCUSD", state=state,
                           predicted=pred.get("prediction"), outcome_up=outcome_up)
    rep = m.report()
    assert rep["observe_only"] is True
    # once it had >= MIN_STATE_N samples it predicted UP for up_streak1 and was right ~90%
    assert rep["predictions_scored"] >= 1
    assert rep["prediction_accuracy"] is not None and rep["prediction_accuracy"] >= 0.8
    assert rep["next_window_prediction"]["BTCUSD"]["prediction"] == "UP"
    assert rep["learned_states"]["BTCUSD"]["up_streak1"]["n"] >= 8


def test_rsi_learns_from_all_signals_forward_return():
    """record_signal_outcome scores raw-signal predictiveness over ALL signals (not just trades)
    and folds the move into the conditional model."""
    m = RSITrendModel()
    m.observe(symbol="BTCUSD", direction="UP", ts=1000.0)
    # 40 UP signals; BTC went up 75% of the time over the horizon
    for i in range(40):
        up = (i % 4 != 0)
        m.record_signal_outcome(symbol="BTCUSD", state="up_streak1", model_pred=None,
                                signal_direction="UP", outcome_up=up)
    rep = m.report()
    assert rep["signals_evaluated"] == 40
    assert abs(rep["signal_direction_hit_rate"] - 0.75) < 1e-6
    assert rep["signal_hit_rate_by_direction"]["UP"]["n"] == 40
    # the move is also folded into the conditional state model
    assert rep["learned_states"]["BTCUSD"]["up_streak1"]["n"] == 40


def test_engine_builds_prediction_from_all_signals_without_trading(tmp_path):
    """Even with NO paper trades, every TradingView signal's 5-min forward BTC move is evaluated
    and feeds the RSI prediction model (history of all signals)."""
    import tempfile
    t0 = 9_990_000.0
    win = PulseWindow(event_id="eX", market_id="mX", slug="s", title="BTC Up or Down",
                      open_ts=t0 + 10_000, close_ts=t0 + 10_300,   # window far in the future: no trades
                      up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 5.0           # steadily rising -> UP signals should look predictive
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, data_dir=str(tmp_path),
                      tradingview_secret=SECRET, tradingview_webhook_port=0,
                      tradingview_allowed_symbols=("BTC/USD", "BTCUSD"),
                      tradingview_signal_horizon_s=20.0)        # short horizon for the test
    eng = PulseEngine(cfg, market_feed=_Mkt(win, deep=True), price_feed=feed)
    # warm the price buffer, then fire several UP signals over time
    for i in range(10):
        eng.tick(now=t0 + i)
    for k in range(5):
        eng.tradingview.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes",
                                           "symbol": "BTC/USD", "direction": "UP",
                                           "event_id": f"fr-{k}"}).encode(), now=t0 + 10 + k)
        eng.tick(now=t0 + 10 + k)
    # advance past the horizon so the forward-return evals resolve (price has risen)
    for k in range(8):
        eng.tick(now=t0 + 40 + k * 5)
    assert eng.ledger.trades == 0                  # never traded (window far in the future)
    rep = eng.status()["tradingview"]["rsi_trend"]
    assert rep["learns_from"] == "all_signals_forward_return"
    assert rep["signals_evaluated"] >= 5           # all signals evaluated despite zero trades
    assert rep["signal_direction_hit_rate"] == 1.0  # rising price -> UP signals all correct


def test_rsi_model_persists_round_trip():
    m = RSITrendModel()
    ts = 1000.0
    for i in range(12):
        m.observe(symbol="BTCUSD", direction="UP", ts=ts); ts += 1
        m.score_and_update(symbol="BTCUSD", state="up_streak1", predicted="UP", outcome_up=True)
    m.record_signal_outcome(symbol="BTCUSD", state="up_streak1", model_pred="UP",
                            signal_direction="UP", outcome_up=True)
    m2 = RSITrendModel()
    m2.load_state(m.to_state())
    assert m2.pred_n == 13 and m2.pred_correct == 13
    assert m2.sig_n == 1 and m2.sig_correct == 1          # raw-signal accumulator persisted
    assert m2.report()["learned_states"]["BTCUSD"]["up_streak1"]["n"] == 13
    assert m2.trend("BTCUSD")["last_direction"] == "UP"


# ============================= engine integration (#6,#7) ================================== #
class _Mkt:
    """Single-window market with a configurable up/down book."""
    def __init__(self, w, *, deep=True):
        self._w = w
        self._deep = deep

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
        else:  # thin book — cannot fully fill -> execution gate rejects (partial_fill_risk)
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=2.0,
                                  bid_depth_usd=2.0, asks=[(0.55, 1.0)], bids=[(0.50, 1.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=2.0,
                                    bid_depth_usd=2.0, asks=[(0.49, 1.0)], bids=[(0.44, 1.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _cfg(tmp_path):
    return PulseConfig(tick_seconds=1.0, size_usd=50.0, min_edge=0.02, basis_buffer=0.0,
                       min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                       settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                       tradingview_secret=SECRET, tradingview_webhook_port=0,
                       tradingview_allowed_symbols=("BTC/USD", "BTCUSD"),
                       directional_down_only=False,
                       directional_series_slugs=(),
                       baseline_cohort_gate_enabled=False,
                       directional_require_winning_bucket=False,
                       data_dir=str(tmp_path))


def _engine(tmp_path, *, deep):
    t0 = 9_800_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    eng = PulseEngine(_cfg(tmp_path), market_feed=_Mkt(win, deep=deep), price_feed=feed)
    return eng, t0


def test_tradingview_feeds_observe_only_feature(tmp_path):
    eng, t0 = _engine(tmp_path, deep=True)
    # a strong DOWN alert arrives while the price is RISING (model would go UP)
    eng.tradingview.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes",
                                       "symbol": "BTC/USD", "direction": "DOWN", "strength": 0.99,
                                       "event_id": "tv-down"}).encode(), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    pos = list(eng.ledger.positions.values())
    assert pos and pos[0].side == "up"        # DOWN alert did not force a DOWN trade
    assert pos[0].external["direction"] == "DOWN"   # signal recorded on the position at entry
    eng.tick(now=t0 + 305)                    # settle the window
    st = eng.status()
    tv = st["tradingview"]
    assert tv["enabled"] is True and tv["tradingview_observe_only"] is True
    assert tv["tradingview_alerts_valid"] == 1
    assert tv["tradingview_latest_signal"]["direction"] == "DOWN"
    # the signal is attached to candidates as an OBSERVE-ONLY feature
    ext = [r.get("external") for r in st["recent_evaluations"] if r.get("external")]
    assert ext and ext[0]["source"] == "tradingview" and ext[0]["observe_only"] is True
    # the settled outcome is attributed to the signal in the edge measurement (observe-only)
    edge = tv["edge_vs_5min_outcome"]
    assert edge["observe_only"] is True and edge["n_settled_with_signal"] == 1
    assert "DOWN" in edge["by_direction"]     # the DOWN signal at entry was recorded


def _gate_cfg(tmp_path, **over):
    return PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
                       min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                       settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                       tradingview_secret=SECRET, tradingview_webhook_port=0,
                       tradingview_allowed_symbols=("BTC/USD", "BTCUSD"),
                       tradingview_signal_gate_enabled=True,
                       directional_down_only=False,
                       directional_series_slugs=(),
                       baseline_cohort_gate_enabled=False,
                       directional_require_winning_bucket=False,
                       data_dir=str(tmp_path), **over)


def _gate_engine(tmp_path):
    t0 = 9_850_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0          # rising -> model wants UP
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    return PulseEngine(_gate_cfg(tmp_path), market_feed=_Mkt(win, deep=True), price_feed=feed), t0


def _drive(eng, t0):
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)


def test_signal_gate_blocks_trade_when_no_signal(tmp_path):
    eng, t0 = _gate_engine(tmp_path)
    _drive(eng, t0)                      # rising price -> bot wants UP, but NO TradingView signal
    assert eng.ledger.trades == 0
    reasons = eng.status()["tick_reasons"]
    assert any("tv_gate_no_signal" in k for k in reasons)
    assert eng.status()["tradingview"]["signal_gate"]["active"] is True


def test_signal_gate_blocks_when_signal_opposes(tmp_path):
    eng, t0 = _gate_engine(tmp_path)
    eng.tradingview.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTC/USD",
                                       "direction": "DOWN", "event_id": "g-down"}).encode(),
                           now=t0 - 6)
    _drive(eng, t0)                      # rising price -> bot wants UP, signal says DOWN -> blocked
    assert eng.ledger.trades == 0
    assert any("tv_gate_opposes_signal" in k for k in eng.status()["tick_reasons"])


def test_signal_gate_allows_aligned_trade(tmp_path):
    eng, t0 = _gate_engine(tmp_path)
    eng.tradingview.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTC/USD",
                                       "direction": "UP", "event_id": "g-up"}).encode(),
                           now=t0 - 6)
    _drive(eng, t0)                      # rising price + UP signal -> trade permitted (gate+exec ok)
    pos = list(eng.ledger.positions.values())
    assert pos and pos[0].side == "up" and eng.ledger.trades >= 1
    assert eng.light_report()["global_reconciled"] is True


def test_tradingview_cannot_bypass_execution_gate(tmp_path):
    # thin book -> the execution gate must reject every candidate, EVEN with a strong UP alert.
    eng, t0 = _engine(tmp_path, deep=False)
    eng.tradingview.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes",
                                       "symbol": "BTC/USD", "direction": "UP", "strength": 1.0,
                                       "event_id": "tv-up"}).encode(), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    # NO paper trade happened despite the strong aligned alert — the gate is the sole authority
    assert eng.ledger.trades == 0
    eg = eng.ledger.exec_gate_stats()
    assert eg["candidates"] >= 1 and eg["accepted"] == 0
    assert eg["rejected"]["partial_fill_risk"] >= 1 and eg["reconciled"] is True
    # the alert was still recorded (observe-only) and reconciliation still holds
    assert eng.status()["tradingview"]["tradingview_alerts_valid"] == 1
    assert eng.light_report()["global_reconciled"] is True


def test_alert_history_keeps_last_ten_per_symbol(tmp_path):
    intake = _intake(tmp_path=tmp_path, alert_history_per_symbol=10)
    now = 1_000_000.0
    for i in range(12):
        code, body = intake.ingest(_alert(event_id="evt-%d" % i, direction="UP" if i % 2 else "DOWN",
                                          bar_time=now - 60 + i), now=now + i)
        assert code == 200 and body.get("accepted") is True
    hist = intake.alert_history_for_symbol("BTCUSD")
    assert len(hist) == 10
    assert hist[0]["event_id"] == "evt-2"
    assert hist[-1]["event_id"] == "evt-11"
    snap = intake.alert_history_snapshot(focus_symbol="BTCUSD")
    assert snap["per_symbol_limit"] == 10
    assert len(snap["by_symbol"]["BTCUSD"]) == 10


def test_alert_history_cap_prefers_configured_over_stale_persisted(tmp_path):
    """Env bump 10→60 must expand deque; stale persisted cap of 10 must not win."""
    small = _intake(tmp_path=tmp_path, alert_history_per_symbol=10)
    now = 3_000_000.0
    for i in range(5):
        small.ingest(_alert(event_id="cap-%d" % i, direction="UP"), now=now + i)
    assert small.alert_history_per_symbol == 10
    big = _intake(tmp_path=tmp_path, alert_history_per_symbol=60)
    assert big.alert_history_per_symbol == 60
    hist = big.alert_history_for_symbol("BTCUSD")
    assert len(hist) == 5
    # Can grow past old cap of 10
    for i in range(5, 15):
        big.ingest(_alert(event_id="cap-%d" % i, direction="DOWN"), now=now + i)
    assert len(big.alert_history_for_symbol("BTCUSD")) == 15


def test_alert_history_persists_across_restart(tmp_path):
    intake = _intake(tmp_path=tmp_path, alert_history_per_symbol=10)
    now = 2_000_000.0
    for i in range(3):
        intake.ingest(_alert(event_id="persist-%d" % i, direction="UP"), now=now + i)
    intake2 = _intake(tmp_path=tmp_path, alert_history_per_symbol=10)
    hist = intake2.alert_history_for_symbol("BTCUSD")
    assert len(hist) == 3
    assert [h["event_id"] for h in hist] == ["persist-0", "persist-1", "persist-2"]


def test_scope_since_clears_pre_epoch_state(tmp_path):
    intake = _intake(tmp_path=tmp_path, alert_history_per_symbol=10)
    epoch = 2_000_000.0
    intake.ingest(_alert(event_id="old-1", direction="UP"), now=epoch - 100)
    intake.ingest(_alert(event_id="old-2", direction="DOWN"), now=epoch - 50)
    intake.ingest(_alert(event_id="new-1", direction="UP"), now=epoch + 1)
    intake.ingest(_alert(event_id="new-2", direction="DOWN"), now=epoch + 2)
    assert intake.received == 4 and intake.valid == 4

    kept = intake.scope_since(epoch)
    assert kept == 2
    rep = intake.report()
    assert rep["tradingview_alerts_received"] == 2
    assert rep["tradingview_alerts_valid"] == 2
    assert rep["tradingview_alerts_rejected"] == 0
    assert len(intake.alert_history_for_symbol("BTCUSD")) == 2
    assert [h["event_id"] for h in intake.alert_history_for_symbol("BTCUSD")] == ["new-1", "new-2"]

    intake2 = _intake(tmp_path=tmp_path, alert_history_per_symbol=10)
    rep2 = intake2.report()
    assert rep2["tradingview_alerts_valid"] == 2
    assert rep2["tradingview_alerts_rejected"] == 0


def test_alert_history_separate_per_symbol(tmp_path):
    intake = TradingViewIntake(secret=SECRET, allowed_symbols=["BTCUSD", "ETHUSD"],
                               bot_name="hermes", max_age_s=90.0, data_dir=str(tmp_path),
                               alert_history_per_symbol=10)
    now = 3_000_000.0
    intake.ingest(_alert(symbol="BTCUSD", event_id="btc-1", direction="UP"), now=now)
    eth = json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "ETHUSD",
                      "direction": "DOWN", "strength": 0.7, "indicator_name": "RSI",
                      "event_id": "eth-1"}).encode()
    intake.ingest(eth, now=now + 1)
    snap = intake.alert_history_snapshot()
    assert len(snap["by_symbol"]["BTCUSD"]) == 1
    assert snap["by_symbol"]["BTCUSD"][0]["direction"] == "UP"
    assert len(snap["by_symbol"]["ETHUSD"]) == 1
    assert snap["by_symbol"]["ETHUSD"][0]["direction"] == "DOWN"
