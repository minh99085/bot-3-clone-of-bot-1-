"""Grok-follow mispricing + edge/TTC + executable-margin gates."""

from __future__ import annotations

from dataclasses import dataclass

from engine.pulse.engine import PulseEngine, PulseConfig


@dataclass
class _FakeEsnap:
    stale_divergence_class: str = "insufficient_data"
    pulse_edge_score_bucket: str = "medium"


def _gate_engine(**cfg_kw) -> PulseEngine:
    defaults = {
        "mispricing_gate_enabled": True,
        "edge_ttc_gate_enabled": True,
        "baseline_up_tv_gate_enabled": True,
    }
    defaults.update(cfg_kw)
    cfg = PulseConfig(**defaults)
    eng = object.__new__(PulseEngine)
    eng.cfg = cfg
    eng._mispricing_gate_counts = {}
    return eng


def _cex_sig(**kw):
    base = {"has_signal": True, "side": "down", "divergence": -0.06, "confirmed": True}
    base.update(kw)
    return base


def _up_strong_tv():
    return {"direction": "UP", "strength": 0.85, "signal_level": "UP_STRONG"}


def test_mispricing_gate_disabled_passes():
    eng = _gate_engine(mispricing_gate_enabled=False)
    ok, _ = eng._mispricing_gate_ok(side="up", cex_sig={}, ttc_s=50.0)
    assert ok is True


def test_mispricing_gate_requires_cex_signal():
    eng = _gate_engine()
    ok, reason = eng._mispricing_gate_ok(side="down", cex_sig={"has_signal": False}, ttc_s=200.0)
    assert ok is False and reason == "misprice_no_cex_signal"


def test_mispricing_gate_requires_side_alignment_and_ttc_window():
    eng = _gate_engine()
    ok, reason = eng._mispricing_gate_ok(side="up", cex_sig=_cex_sig(side="down"), ttc_s=200.0)
    assert ok is False and reason == "misprice_side_mismatch"
    ok, reason = eng._mispricing_gate_ok(side="down", cex_sig=_cex_sig(), ttc_s=120.0)
    assert ok is False and reason == "misprice_ttc_out_of_window"
    ok, _ = eng._mispricing_gate_ok(
        side="down", cex_sig=_cex_sig(), ttc_s=210.0,
        esnap=_FakeEsnap("stale_polymarket_down"))
    assert ok is True


def test_mispricing_gate_requires_confirmation():
    eng = _gate_engine()
    ok, reason = eng._mispricing_gate_ok(
        side="down", cex_sig=_cex_sig(confirmed=False), ttc_s=200.0)
    assert ok is False and reason == "misprice_not_confirmed"


def test_mispricing_gate_down_requires_stale_polymarket_down():
    eng = _gate_engine()
    ok, reason = eng._mispricing_gate_ok(
        side="down", cex_sig=_cex_sig(), ttc_s=200.0, esnap=_FakeEsnap("not_stale"))
    assert ok is False and reason == "misprice_stale_down_required"
    ok, _ = eng._mispricing_gate_ok(
        side="down", cex_sig=_cex_sig(), ttc_s=200.0,
        esnap=_FakeEsnap("stale_polymarket_down"))
    assert ok is True


def test_edge_ttc_gate_blocks_mid_window_low_score():
    eng = _gate_engine()
    ok, reason = eng._edge_ttc_gate_ok(esnap=_FakeEsnap(pulse_edge_score_bucket="medium"),
                                       ttc_s=120.0)
    assert ok is False and reason == "edge_ttc_mid_window_low_score"
    ok, _ = eng._edge_ttc_gate_ok(esnap=_FakeEsnap(pulse_edge_score_bucket="high"), ttc_s=120.0)
    assert ok is True
    ok, _ = eng._edge_ttc_gate_ok(esnap=_FakeEsnap(pulse_edge_score_bucket="low"), ttc_s=200.0)
    assert ok is True


def test_edge_ttc_gate_blocks_late_window_low_score():
    eng = _gate_engine()
    ok, reason = eng._edge_ttc_gate_ok(esnap=_FakeEsnap(pulse_edge_score_bucket="medium"),
                                       ttc_s=250.0)
    assert ok is False and reason == "edge_ttc_late_window_low_score"
    ok, _ = eng._edge_ttc_gate_ok(esnap=_FakeEsnap(pulse_edge_score_bucket="high"), ttc_s=250.0)


def test_15m_mispricing_and_edge_ttc_scale_with_window():
    eng = _gate_engine(mispricing_ttc_min_s=60.0, mispricing_ttc_max_s=480.0,
                       mispricing_gate_enabled=True, edge_ttc_gate_enabled=True)
    stale = _FakeEsnap(stale_divergence_class="stale_polymarket_down")
    ok, _ = eng._mispricing_gate_ok(
        side="down", cex_sig=_cex_sig(), ttc_s=500.0, esnap=stale, window_seconds=900)
    assert ok
    ok, reason = eng._mispricing_gate_ok(
        side="down", cex_sig=_cex_sig(), ttc_s=100.0, esnap=stale, window_seconds=900)
    assert not ok and reason == "misprice_ttc_out_of_window"
    ok, reason = eng._edge_ttc_gate_ok(
        esnap=_FakeEsnap(pulse_edge_score_bucket="medium"), ttc_s=400.0, window_seconds=900)
    assert not ok and reason == "edge_ttc_mid_window_low_score"
    ok, _ = eng._edge_ttc_gate_ok(
        esnap=_FakeEsnap(pulse_edge_score_bucket="high"), ttc_s=400.0, window_seconds=900)
    assert ok


def test_executable_mispricing_margin():
    eng = _gate_engine(mispricing_min_executable_margin=0.03, edge_buffer=0.01)
    ok, reason = eng._executable_mispricing_ok(p_win=0.58, ask=0.55)
    assert ok is False and reason == "misprice_executable_margin_low"
    ok, _ = eng._executable_mispricing_ok(p_win=0.62, ask=0.55)
    assert ok is True


def test_mispricing_follow_entry_on_abstain():
    eng = _gate_engine(mispricing_ttc_min_s=90.0, mispricing_ttc_max_s=300.0,
                       mispricing_follow_on_abstain=True)
    eng.grok_decider = type("G", (), {"report": lambda self: {"graded_directional": 30,
                                                               "direction_accuracy": 0.55}})()
    sig = {"has_signal": True, "side": "up", "divergence": 0.12, "confirmed": True,
           "cex_p_up": 0.62}
    esnap = _FakeEsnap("not_stale")
    esnap.pulse_edge_score_bucket = "high"
    esnap.cex_agreement_bucket = "strong"
    tv = _up_strong_tv()
    assert eng._mispricing_follow_entry(sig, 210.0, esnap, tv) is None
    assert eng._mispricing_gate_counts.get("misprice_up_side_disabled") == 1
    down_sig = {"has_signal": True, "side": "down", "divergence": -0.12, "confirmed": True,
                "cex_p_up": 0.38}
    down_esnap = _FakeEsnap("stale_polymarket_down")
    down_esnap.pulse_edge_score_bucket = "high"
    entry = eng._mispricing_follow_entry(down_sig, 210.0, down_esnap, tv)
    assert entry is not None and entry["side"] == "down"
    assert eng._mispricing_follow_entry(down_sig, 50.0, down_esnap, tv) is None


def test_mispricing_follow_up_side_disabled():
    eng = _gate_engine(mispricing_ttc_min_s=90.0, mispricing_ttc_max_s=240.0,
                       mispricing_follow_on_abstain=True)
    sig = {"has_signal": True, "side": "up", "divergence": 0.12, "confirmed": True,
           "cex_p_up": 0.62}
    esnap = _FakeEsnap("not_stale")
    esnap.pulse_edge_score_bucket = "high"
    esnap.cex_agreement_bucket = "strong"
    assert eng._mispricing_follow_entry(sig, 200.0, esnap, _up_strong_tv()) is None
    assert eng._mispricing_gate_counts.get("misprice_up_side_disabled") == 1


def test_mispricing_follow_entry_disabled():
    eng = _gate_engine(mispricing_follow_on_abstain=False)
    sig = {"has_signal": True, "side": "up", "divergence": 0.12, "confirmed": True,
           "cex_p_up": 0.62}
    assert eng._mispricing_follow_entry(sig, 200.0) is None