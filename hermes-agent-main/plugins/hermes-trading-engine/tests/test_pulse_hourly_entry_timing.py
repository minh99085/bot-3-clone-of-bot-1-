"""Learned 1h entry-timing gate — bucket mapping, hard floor, and statistical rejects."""

from __future__ import annotations

from engine.pulse.hourly_entry_timing import (
    HourlyEntryEvidence,
    LearnedHourlyEntryGate,
    hourly_entry_bucket,
    is_hourly_window,
)


def test_hourly_entry_bucket_mapping():
    assert hourly_entry_bucket(30, window_seconds=3600) == "h0_5m"
    assert hourly_entry_bucket(299, window_seconds=3600) == "h0_5m"
    assert hourly_entry_bucket(300, window_seconds=3600) == "h5_15m"
    assert hourly_entry_bucket(600, window_seconds=3600) == "h5_15m"
    assert hourly_entry_bucket(1200, window_seconds=3600) == "h15_30m"
    assert hourly_entry_bucket(2000, window_seconds=3600) == "h30_45m"
    assert hourly_entry_bucket(3000, window_seconds=3600) == "h45_60m"
    assert hourly_entry_bucket(None, window_seconds=3600) == "na"
    assert hourly_entry_bucket(60, window_seconds=300) == "na"


def test_is_hourly_window():
    assert is_hourly_window(3600) is True
    assert is_hourly_window(7200) is True
    assert is_hourly_window(900) is False
    assert is_hourly_window(None) is False


def _losing_bucket_evidence(bucket: str, *, n: int = 30) -> HourlyEntryEvidence:
    ev = HourlyEntryEvidence()
    for i in range(n):
        won = i < int(n * 0.30)
        ev.record(bucket, won=won, pnl=(3.0 if won else -5.0))
    return ev


def test_hard_floor_rejects_too_early():
    gate = LearnedHourlyEntryGate(enabled=True, min_seconds_since_open=180.0,
                                  exploration_rate=0.0)
    ev = HourlyEntryEvidence()
    res = gate.evaluate(window_seconds=3600, seconds_since_open=60.0, evidence=ev)
    assert res["decision"] == "reject"
    assert res["reasons"] == ["hourly_too_early"]
    assert gate.too_early == 1


def test_hard_floor_allows_after_min_seconds():
    gate = LearnedHourlyEntryGate(enabled=True, min_seconds_since_open=180.0,
                                  exploration_rate=0.0)
    ev = HourlyEntryEvidence()
    res = gate.evaluate(window_seconds=3600, seconds_since_open=200.0, evidence=ev)
    assert res["decision"] == "accept"
    assert res["bucket"] == "h0_5m"


def test_hard_ceiling_rejects_too_late():
    gate = LearnedHourlyEntryGate(
        enabled=True,
        min_seconds_since_open=900.0,
        max_seconds_since_open=2700.0,
        exploration_rate=0.0,
    )
    ev = HourlyEntryEvidence()
    res = gate.evaluate(window_seconds=3600, seconds_since_open=2800.0, evidence=ev)
    assert res["decision"] == "reject"
    assert res["reasons"] == ["hourly_too_late"]
    assert gate.too_late == 1


def test_target_band_accepts_mid_hour():
    gate = LearnedHourlyEntryGate(
        enabled=True,
        min_seconds_since_open=900.0,
        max_seconds_since_open=2700.0,
        exploration_rate=0.0,
    )
    ev = HourlyEntryEvidence()
    res = gate.evaluate(window_seconds=3600, seconds_since_open=1800.0, evidence=ev)
    assert res["decision"] == "accept"
    assert res["bucket"] == "h30_45m"


def test_proven_losing_bucket_rejected():
    gate = LearnedHourlyEntryGate(min_samples=20, exploration_rate=0.0, seed=1)
    ev = _losing_bucket_evidence("h5_15m", n=30)
    res = gate.evaluate(window_seconds=3600, seconds_since_open=600.0, evidence=ev)
    assert res["decision"] == "reject"
    assert res["reasons"][0].startswith("bad_hourly_bucket:h5_15m")


def test_cold_bucket_passes():
    gate = LearnedHourlyEntryGate(min_samples=20, exploration_rate=0.0)
    ev = _losing_bucket_evidence("h5_15m", n=5)   # below min_samples
    res = gate.evaluate(window_seconds=3600, seconds_since_open=600.0, evidence=ev)
    assert res["decision"] == "accept"


def test_non_hourly_window_bypasses_gate():
    gate = LearnedHourlyEntryGate(enabled=True, min_seconds_since_open=180.0)
    ev = HourlyEntryEvidence()
    res = gate.evaluate(window_seconds=900, seconds_since_open=10.0, evidence=ev)
    assert res["decision"] == "accept"
    assert "gate_disabled_or_not_hourly" in res["reasons"]


def test_exploration_on_bad_bucket():
    gate = LearnedHourlyEntryGate(min_samples=20, exploration_rate=0.5, seed=42)
    ev = _losing_bucket_evidence("h15_30m", n=30)
    explored = rejected = 0
    for _ in range(200):
        d = gate.evaluate(window_seconds=3600, seconds_since_open=1200.0, evidence=ev)["decision"]
        explored += int(d == "explore")
        rejected += int(d == "reject")
    assert explored > 0 and rejected > 0


def test_evidence_persists_state():
    ev = HourlyEntryEvidence()
    ev.record("h0_5m", won=True, pnl=2.0)
    ev.record("h0_5m", won=False, pnl=-5.0)
    st = ev.to_state()
    ev2 = HourlyEntryEvidence()
    ev2.load_state(st)
    stat = ev2.stat("h0_5m")
    assert stat["n"] == 2
    assert stat["pnl_usd"] == -3.0
