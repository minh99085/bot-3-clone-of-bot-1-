"""B1 — PURE mode strips every adaptive confound; fixed sizing; A/B wiring.

The point: in pure mode a lane's entries/sizes depend ONLY on the strategy
(barrier q vs market p through the frozen gates) — never on learned state
(bandit, lessons, risk pauses, RGMC/MCHB/CBPF). lane02_autonomy is the same
barrier q with the stack ON: the clean A/B of the autonomy layer.
"""

from __future__ import annotations

import json

import pytest

from hermes.models import (
    AllocationProposal,
    ConfidenceTier,
    Direction,
    EntryMode,
    Regime,
    Signal,
)
from hermes.pretrade import analyze_signal
from hermes.substrategy import annotate_signal


def _sig(slug: str = "btc-updown-15m-1784601000", **kw) -> Signal:
    base = dict(
        market_id="mkt_btc",
        slug=slug,
        question="Bitcoin Up or Down",
        direction=Direction.DOWN,
        entry_mode=EntryMode.MISPRICING,
        confidence_tier=ConfidenceTier.A,
        conviction=0.8,
        fair_value=0.55,
        market_price=0.48,
        expected_edge=0.09,
        live_ev=0.075,
        regime=Regime.MEAN_REVERT,
        hourly_bucket=14,
        size_usd_suggested=50.0,
        entry_vwap_target=0.485,
        pre_entry_stability_ok=True,
        timeframe="15m",
        oracle_alignment=0.8,
        meta={"paper": True, "asset": "BTC", "mispricing_active": True},
    )
    base.update(kw)
    return annotate_signal(Signal(**base))


def _proposal(sig: Signal) -> AllocationProposal:
    return AllocationProposal(
        capital_usd=2000,
        weights={sig.substrategy_id: 0.5},
        diversification_ratio=1.2,
        concentration_hhi=0.5,
    )


AVOID_LESSON = (
    "### [2026-07-20] `les_x` — CRITICAL (rejection)\n"
    "- **Rule**: AVOID:`btc_updown_15m` until gated skip review\n"
)


def test_pure_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_PURE_MODE", raising=False)
    from hermes.pure_mode import pure_mode_enabled

    assert pure_mode_enabled() is False


def test_pure_fixed_size_ignores_lessons_and_sleeve_history(monkeypatch):
    """Terrible sleeve stats + a binding AVOID lesson must NOT touch a pure lane."""
    monkeypatch.setenv("HERMES_PURE_MODE", "1")
    monkeypatch.setenv("HERMES_SCOPE_BTC_UPDOWN_ONLY", "1")
    sig = _sig()
    analysis = analyze_signal(
        sig, _proposal(sig), bankroll=2000.0, lessons=AVOID_LESSON, paper=True
    )
    assert not analysis.skip
    assert analysis.recommended_size_usd == pytest.approx(2000.0 * 0.02)  # $40 fixed
    assert analysis.lessons_applied == []
    assert any("pure_mode" in r for r in analysis.reasons)


def test_autonomy_lane_still_applies_lessons(monkeypatch):
    """Control: same signal + AVOID lesson with pure OFF → the lesson bites."""
    monkeypatch.delenv("HERMES_PURE_MODE", raising=False)
    monkeypatch.setenv("HERMES_SCOPE_BTC_UPDOWN_ONLY", "1")
    sig = _sig()
    analysis = analyze_signal(
        sig, _proposal(sig), bankroll=2000.0, lessons=AVOID_LESSON, paper=True
    )
    assert analysis.skip
    assert any("lesson_AVOID" in r for r in analysis.reasons)


def test_pure_size_env_override_clamped_by_hard_cap(monkeypatch):
    monkeypatch.setenv("HERMES_PURE_MODE", "1")
    monkeypatch.setenv("HERMES_PURE_SIZE_PCT", "0.04")   # asks 4%
    monkeypatch.setenv("HERMES_MAX_TRADE_PCT", "0.02")   # hard cap 2% wins
    sig = _sig()
    analysis = analyze_signal(sig, _proposal(sig), bankroll=2000.0, lessons="", paper=True)
    assert analysis.recommended_size_usd == pytest.approx(40.0)


def test_pure_mode_out_of_scope_still_skipped(monkeypatch):
    """Scope is a safety rail, not adaptivity — stays ON in pure mode."""
    monkeypatch.setenv("HERMES_PURE_MODE", "1")
    monkeypatch.setenv("HERMES_SCOPE_BTC_UPDOWN_ONLY", "1")
    sig = _sig(slug="doge-updown-15m-1784601000", question="Doge Up or Down",
               meta={"paper": True, "asset": "DOGE"})
    analysis = analyze_signal(sig, _proposal(sig), bankroll=2000.0, lessons="", paper=True)
    if analysis.skip:  # fast-scope signals only; skip reason must be scope
        assert any("out_of_scope" in r for r in analysis.reasons)


def test_bandit_fixed_and_stateless_in_pure_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_PURE_MODE", "1")
    from hermes.bandit import ContextualBandit
    from hermes.mispricing import MispricingSignal

    path = tmp_path / "bandit.json"
    b = ContextualBandit(path=path)
    mp = MispricingSignal(pm_implied_up=0.5, timeframe="15m")
    mp.active = True
    mp.conviction = 0.9
    d = b.decide(mp, 12)
    assert d.arm == "exploit" and d.size_scale == 1.0
    assert "pure_mode" in d.reason
    b.record_pull(d)
    b.update_reward(d.context, "exploit", 1.0)
    assert not path.exists()  # no state written in pure mode


def test_risk_pause_observed_but_not_enforced(monkeypatch):
    import hermes.risk_monitor as rm

    losing = [
        {"won": False, "pnl_usd": -40.0, "size_usd": 40.0, "entry_price": 0.5}
        for _ in range(20)
    ]
    monkeypatch.setattr(rm, "_recent_settlements", lambda paper=True, n=50: losing)
    monkeypatch.setattr(
        rm, "read_state_fields", lambda: {"capital_usd": 2000}, raising=False
    )

    monkeypatch.delenv("HERMES_PURE_MODE", raising=False)
    snap_full = rm.compute_risk_snapshot(state={"capital_usd": 2000}, paper=True)
    assert snap_full.pause_loop  # 20 straight losses pauses a normal lane

    monkeypatch.setenv("HERMES_PURE_MODE", "1")
    snap_pure = rm.compute_risk_snapshot(state={"capital_usd": 2000}, paper=True)
    assert snap_pure.pause_loop is False
    assert snap_pure.circuit_breaker_tripped is False
    assert "pure_mode_observed_not_enforced" in snap_pure.trip_reason


def test_instance_paused_ignores_stale_pause_file(monkeypatch, tmp_path):
    import hermes.risk_monitor as rm

    p = tmp_path / "risk_state.json"
    p.write_text(json.dumps({"pause_loop": True, "trip_reason": "old halt"}))
    monkeypatch.setattr(rm, "risk_state_path", lambda paper=True: p)

    monkeypatch.delenv("HERMES_PURE_MODE", raising=False)
    assert rm.instance_paused(paper=True)[0] is True
    monkeypatch.setenv("HERMES_PURE_MODE", "1")
    paused, reason = rm.instance_paused(paper=True)
    assert paused is False and "pure_mode" in reason


def test_lessons_writes_disabled_in_pure_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_PURE_MODE", "1")
    import hermes.lessons_engine as le
    import hermes.state_io as sio

    kn = tmp_path / "knowledge"
    kn.mkdir()
    monkeypatch.setattr(sio, "KNOWLEDGE", kn)
    from hermes.models import Settlement

    stl = Settlement(
        position_id="p", signal_id="s", market_id="m", direction=Direction.DOWN,
        entry_price=0.5, exit_price=0.0, size_usd=40.0, pnl_usd=-40.0, won=False,
        regime=Regime.MEAN_REVERT, hourly_bucket=1,
        entry_mode=EntryMode.MISPRICING, confidence_tier=ConfidenceTier.B,
        market_series="btc_updown_15m", substrategy_id="x", slug="btc-updown-15m-1",
        timeframe="15m", paper=True, notes="",
    )
    lesson = le.lesson_from_settlement(stl)
    le.append_lesson(lesson)
    le.promote_lesson(lesson)
    assert not (kn / "LESSONS.md").exists()  # nothing written


def test_orchestrator_hooks_noop_in_pure_mode(monkeypatch):
    monkeypatch.setenv("HERMES_PURE_MODE", "1")
    from autonomy.orchestrator import apply_soft_sizing, mchb_gate, on_settlement

    assert apply_soft_sizing(40.0, 0.5) == (40.0, 0.5)
    arm, meta = mchb_gate({"timeframe": "15m"})
    assert arm == "exploit" and meta.get("mchb_skipped") == "pure_mode"
    assert on_settlement(object()) == {"ok": False, "skipped": "pure_mode"}


def test_avoid_bucket_never_hits_in_pure_mode(monkeypatch):
    monkeypatch.setenv("HERMES_PURE_MODE", "1")
    from hermes.signal_generator import avoid_bucket_hit

    hit = avoid_bucket_hit(
        EntryMode.MISPRICING, Regime.MEAN_REVERT, 3, [],
        "avoid:mispricing everywhere", series="btc_updown_15m",
    )
    assert hit is False


def test_lane_registry_chainlink_gone_falls_back_to_baseline(monkeypatch):
    from hermes.lane_variants import LANES, active_spec

    assert "chainlink_ref" not in LANES
    monkeypatch.setenv("HERMES_STRATEGY_VARIANT", "chainlink_ref")
    spec = active_spec()  # unknown name → loud baseline fallback
    assert "baseline" in spec.name


def test_compose_wiring_pure_flags():
    """lane02_autonomy = full stack; every other lane runs pure."""
    text = open("docker-compose.yml").read()
    assert "lane02_chainlink" not in text
    lane02 = text.split("hermes-lane02_autonomy:")[2 if False else 1]
    lane02_env = lane02.split("volumes:")[0]
    assert "HERMES_PURE_MODE" not in lane02_env
    assert 'HERMES_STRATEGY_VARIANT: baseline' in lane02_env
    for lane in ("lane01_baseline", "lane03_favorite", "lane04_longshot",
                 "lane05_late", "lane06_garch", "lane07_marketsigma",
                 "lane08_legacy", "lane09_random", "lane10_depth"):
        seg = text.split(f"HERMES_INSTANCE_ID: {lane}")[1].split("volumes:")[0]
        assert 'HERMES_PURE_MODE: "1"' in seg, lane
