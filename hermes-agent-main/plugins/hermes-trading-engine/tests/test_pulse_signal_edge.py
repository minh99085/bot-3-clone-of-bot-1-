"""WS1 — unified signal-edge ledger: FOLLOW / FADE / OBSERVE on real settled outcomes.

Proves: a confidently anti-predictive signal (hit-rate < 0.5, Wilson upper < 0.5) is flagged FADE;
a reliably-right signal is FOLLOW; a coin-flip or thin sample is OBSERVE; the report extractor maps
the live signal sections; and the live ledger accumulates + persists. Everything OBSERVE-ONLY.
"""

from __future__ import annotations

from engine.pulse.signal_edge import (wilson_bounds, classify_signal, build_signal_edge_summary,
                                       extract_signal_edge_entries, SignalEdgeLedger,
                                       FOLLOW, FADE, OBSERVE)


def test_wilson_bounds_basic():
    assert wilson_bounds(0, 0) == (0.0, 1.0)            # unknown = widest
    lo, hi = wilson_bounds(80, 200)                     # 40% over 200
    assert 0.30 < lo < 0.40 and 0.45 < hi < 0.50        # upper still below 0.5


def test_anti_predictive_signal_is_faded():
    r = classify_signal(200, 0.40, min_samples=50)
    assert r["verdict"] == FADE and r["wilson_hi"] < 0.5
    assert r["affects_trading"] is False


def test_reliably_right_signal_is_followed():
    r = classify_signal(200, 0.62, min_samples=50)
    assert r["verdict"] == FOLLOW and r["wilson_lo"] > 0.5


def test_coinflip_and_thin_are_observe():
    assert classify_signal(500, 0.50, min_samples=50)["verdict"] == OBSERVE   # straddles
    thin = classify_signal(5, 0.0, min_samples=50)
    assert thin["verdict"] == OBSERVE and "insufficient" in thin["reason"]    # no lucky promote


def test_summary_buckets_follow_and_fade():
    entries = [
        {"source": "rsi_trend", "n": 203, "accuracy": 0.43},        # anti-predictive -> fade
        {"source": "grok_predictor", "n": 182, "accuracy": 0.40},   # anti-predictive -> fade
        {"source": "grok_decider", "context": "hurst=trending", "n": 120, "accuracy": 0.66},  # follow
        {"source": "tv_composite", "n": 10, "accuracy": 0.30},      # thin -> observe (not faded)
    ]
    rep = build_signal_edge_summary(entries, min_samples=50)
    assert rep["observe_only"] is True and rep["affects_trading"] is False
    fades = {(c["source"], c["context"]) for c in rep["fade_candidates"]}
    follows = {(c["source"], c["context"]) for c in rep["follow_candidates"]}
    assert ("rsi_trend", "all") in fades and ("grok_predictor", "all") in fades
    assert ("grok_decider", "hurst=trending") in follows
    assert ("tv_composite", "all") not in fades                    # thin sample never faded


def test_extract_from_live_report_sections():
    entries = extract_signal_edge_entries(
        tradingview={"rsi_trend": {"hit_rate": 0.4286, "n": 203},
                     "edge_vs_5min_outcome": {"signal_hit_rate": 0.439, "n_settled_with_signal": 196}},
        grok_decider={"direction_accuracy": 0.6667, "decided": 90,
                      "accuracy_by_context": {"hurst_regime": {"trending": {"n": 70, "accuracy": 0.71}}}},
        grok_signal_intel={"predictor_B": {"scored": 182, "accuracy": 0.3956}},
        cex_lead_edge={"buckets": {">=0.30": {"n": 60, "acc": 0.55, "avg_pnl": 0.05}}})
    srcs = {e["source"] for e in entries}
    assert {"rsi_trend", "tradingview_composite", "grok_predictor", "grok_decider", "cex_lead"} <= srcs
    # the negative-alpha predictor is present with its real hit-rate
    pred = next(e for e in entries if e["source"] == "grok_predictor")
    assert pred["n"] == 182 and abs(pred["accuracy"] - 0.3956) < 1e-6


def test_ledger_record_and_persist():
    led = SignalEdgeLedger(min_samples=10)
    # a signal that is right only 30% of the time -> should fade once enough samples
    for i in range(20):
        led.record("rsi_trend", predicted_up=True, outcome_up=(i % 10 < 3))   # 30% correct
    rep = led.report()
    cell = rep["sources"]["rsi_trend"]["all"]
    assert cell["n"] == 20 and cell["verdict"] in (FADE, OBSERVE)
    # round-trip
    led2 = SignalEdgeLedger()
    led2.load_state(led.to_state())
    assert led2.report()["sources"]["rsi_trend"]["all"]["n"] == 20


def test_missing_fields_are_skipped():
    entries = extract_signal_edge_entries(tradingview={"rsi_trend": {"hit_rate": None, "n": 100}})
    assert entries == []                                # no accuracy -> skipped, no crash
    assert classify_signal(None, None)["verdict"] == OBSERVE
