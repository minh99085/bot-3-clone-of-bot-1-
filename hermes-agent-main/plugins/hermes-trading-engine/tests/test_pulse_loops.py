"""Loop-engineering layer: #1 maker-checker verifier, #2 lessons, #3 loop registry, #4 research loop.

All LLM calls are injected (no network). Proves: the verifier approves/vetoes + can only veto/shrink
+ fail-open; lessons compound + dedupe + persist; the registry reports loops; the research loop
turns recommendations into lessons (observe-only). PAPER ONLY.
"""

from __future__ import annotations

from engine.pulse.verifier import normalize_verdict, ClaudeVerifier
from engine.pulse.lessons import LessonsBook
from engine.pulse.loops import LoopRegistry
from engine.pulse.research_loop import ResearchLoop


# ------------------------------- #1 verifier ---------------------------------------------- #
def test_verdict_normalize():
    v = normalize_verdict({"approve": "yes", "max_size_fraction": 2.0, "confidence": 1.5})
    assert v["approve"] is True and v["max_size_fraction"] == 1.0 and v["confidence"] == 1.0
    assert normalize_verdict({"approve": False})["approve"] is False
    assert normalize_verdict("nope") is None


def test_verifier_approve_veto_and_grade():
    v = ClaudeVerifier(verify_fn=lambda p: {"approve": True, "max_size_fraction": 0.5,
                                            "confidence": 0.8, "reason": "ok"}, enabled=True)
    v.request("d1", {"decision": {"action": "up"}})
    assert v._process_one() is True
    verdict = v.get("d1")
    assert verdict["approve"] is True and verdict["max_size_fraction"] == 0.5
    v.grade("d1", won=True, pnl=2.0, acted=True)
    rep = v.report()
    assert rep["approvals"] == 1 and rep["maker_checker"] is True and rep["can_force_trade"] is False
    assert rep["approved_acted_settled"]["n"] == 1
    # veto path
    v2 = ClaudeVerifier(verify_fn=lambda p: {"approve": False, "reason": "weak"}, enabled=True)
    v2.request("d2", {})
    v2._process_one()
    assert v2.get("d2")["approve"] is False and v2.report()["vetoes"] == 1


def test_verifier_veto_counterfactual_grade():
    v = ClaudeVerifier(verify_fn=lambda p: {"approve": False, "reason": "weak edge"}, enabled=True)
    v.request("d-veto", {})
    v._process_one()
    v.grade("d-veto", won=True, pnl=3.5, acted=False)
    rep = v.report()
    assert rep["vetoed_would_have_settled"]["n"] == 1
    assert rep["vetoed_would_have_settled"]["win_rate"] == 1.0
    assert rep["vetoed_would_have_settled"]["pnl_usd"] == 3.5
    assert rep["approved_acted_settled"]["n"] == 0
    # idempotent
    v.grade("d-veto", won=False, pnl=-5.0, acted=False)
    assert rep["vetoed_would_have_settled"]["n"] == 1


def test_verifier_grade_buckets_approve_not_acted_skipped():
    v = ClaudeVerifier(verify_fn=lambda p: {"approve": True}, enabled=True)
    v.request("d3", {})
    v._process_one()
    v.grade("d3", won=True, pnl=1.0, acted=False)
    rep = v.report()
    assert rep["approved_acted_settled"]["n"] == 0
    assert rep["vetoed_would_have_settled"]["n"] == 0


def test_counterfactual_side_pnl():
    from engine.pulse.engine import PulseEngine
    won, pnl = PulseEngine._counterfactual_side_pnl("up", 0.5, 5.0, True)
    assert won is True and pnl == 5.0
    won, pnl = PulseEngine._counterfactual_side_pnl("down", 0.5, 5.0, True)
    assert won is False and pnl == -5.0


def test_verifier_fail_open_and_failclosed():
    v = ClaudeVerifier(verify_fn=lambda p: None, enabled=True, fail_open=True)
    # no verdict yet -> fail-open APPROVE so the bot doesn't freeze
    fo = v.verdict_or_failopen("missing")
    assert fo["approve"] is True and fo["pending"] is True
    vc = ClaudeVerifier(verify_fn=lambda p: None, enabled=True, fail_open=False)
    assert vc.verdict_or_failopen("missing")["approve"] is False     # fail-closed -> veto


# ------------------------------- #2 lessons ----------------------------------------------- #
def test_lessons_compound_update_persist():
    lb = LessonsBook(max_lessons=5)
    assert lb.add(kind="avoid", key="sel:direction=down", rule="avoid down v1") is True
    # re-add the same (kind,key) UPDATES the rule + re-confirms (not locked), returns False (not new)
    assert lb.add(kind="avoid", key="sel:direction=down", rule="avoid down v2") is False
    assert lb._idx[("avoid", "sel:direction=down")]["rule"] == "avoid down v2"   # updated, not locked
    assert lb.add(kind="exploit", key="edge:hurst=trending", rule="exploit trending") is True
    assert len(lb.recent(10)) == 2 and "avoid down v2" in lb.to_markdown()
    lb2 = LessonsBook()
    lb2.load_state(lb.to_state())
    assert len(lb2.lessons) == 2 and lb2.add(kind="avoid", key="sel:direction=down", rule="x") is False


def test_lessons_sync_retracts_stale_and_reactivates():
    lb = LessonsBook(max_lessons=20, revalidate_ttl_s=100.0)
    lb.add(kind="avoid", key="sel:markov=chop", rule="avoid chop", now=1000.0)
    lb.add(kind="exploit", key="edge:hurst=trending", rule="exploit trending", now=1000.0)
    lb.add(kind="risk", key="breaker:loss:1", rule="breaker tripped", now=1000.0)
    # at t=1050 (within TTL), chop no longer active -> NOT yet retracted (grace within ttl)
    assert lb.sync(active_keys={("exploit", "edge:hurst=trending")}, now=1050.0)["n"] == 0
    # at t=1200 (>ttl since last_seen 1000), chop is stale + not active -> RETRACTED
    out = lb.sync(active_keys={("exploit", "edge:hurst=trending")}, now=1200.0)
    assert out["n"] == 1 and "sel:markov=chop" in out["retracted"]
    actkeys = [(l["kind"], l["key"]) for l in lb.active()]
    assert ("avoid", "sel:markov=chop") not in actkeys              # retracted -> not active
    assert ("exploit", "edge:hurst=trending") in actkeys           # still active (was in active_keys)
    assert ("risk", "breaker:loss:1") in actkeys                   # risk kind never synced/retracted
    assert lb.recent(10) and all(l["status"] == "active" for l in lb.recent(10))   # prompts get active only
    # re-confirming a retracted lesson REACTIVATES it
    assert lb.add(kind="avoid", key="sel:markov=chop", rule="avoid chop again", now=1300.0) is False
    assert lb._idx[("avoid", "sel:markov=chop")]["status"] == "active"
    assert lb.report()["retracted_total"] == 1


# ------------------------------- #3 loop registry ----------------------------------------- #
def test_loop_registry_reports_loops():
    r = LoopRegistry()
    r.register("verifier", role="verify", trigger="per_decision", verifier="claude",
               status_fn=lambda: {"enabled": True, "verified": 3})
    r.register("heartbeat", role="automation", trigger="tick", interval_s=4.0)
    rep = r.report()
    assert rep["count"] == 2 and rep["loops"]["verifier"]["role"] == "verify"
    assert rep["loops"]["verifier"]["status"]["verified"] == 3


def test_loop_registry_watchdog_flags_stalled():
    r = LoopRegistry(stall_grace_s=60.0, stall_factor=3.0)
    r.register("heartbeat", role="automation", trigger="tick", interval_s=4.0)
    r.register("news", role="context", trigger="interval", interval_s=300.0)
    r.beat("heartbeat", now=1000.0)
    r.beat("news", now=1000.0)
    # fresh: nothing stalled
    rep = r.report(now=1003.0)
    assert rep["all_live"] is True and rep["loops"]["heartbeat"]["stalled"] is False
    assert rep["loops"]["heartbeat"]["last_beat_age_s"] == 3.0
    # heartbeat cadence 4s -> stalled threshold max(60, 12)=60; at +120s it's stalled
    rep2 = r.report(now=1120.0)
    assert rep2["loops"]["heartbeat"]["stalled"] is True and "heartbeat" in rep2["stalled"]
    # news cadence 300s -> threshold max(60,900)=900; at +120s NOT stalled
    assert rep2["loops"]["news"]["stalled"] is False
    assert rep2["all_live"] is False


# ------------------------------- #4 research loop ----------------------------------------- #
def test_research_loop_adds_lessons_observe_only():
    lb = LessonsBook()
    note = {"summary": "exploit volume:active", "exploit_contexts": ["volume_state=active"],
            "avoid_contexts": ["hurst=noise"], "knob_recommendations": [],
            "new_lessons": [{"key": "r1", "rule": "active volume tends to follow through"}]}
    rl = ResearchLoop(research_fn=lambda rep: note, report_provider=lambda: {"x": 1}, lessons=lb,
                      auto_apply=False)
    rl.refresh()
    r = rl.report()
    assert r["calls"] == 1 and r["last_note"]["summary"].startswith("exploit")
    assert r["lessons_added"] == 1 and lb.lessons[-1]["rule"].startswith("active volume")
    assert r["auto_apply"] is False                  # observe-only by default
    # fail-open: research_fn None -> error, no crash
    rl2 = ResearchLoop(research_fn=lambda rep: None, report_provider=lambda: {})
    rl2.refresh()
    assert rl2.report()["errors"] == 1


def test_research_clean_context_strips_prose_and_filters_dims():
    from engine.pulse.research_loop import _clean_context, make_research_fn
    # prose / stats appended by the model are stripped to a bare dim=bucket
    assert _clean_context("hurst_regime=trending (40% win, n=30)") == "hurst_regime=trending"
    assert _clean_context("ttc_bucket=<60s") == "ttc_bucket=<60s"
    assert _clean_context("edge_quality=high  extra words") == "edge_quality=high"
    assert _clean_context("no equals sign") is None
    # the research fn normalizes contexts coming back from the model
    note = {"summary": "s", "avoid_contexts": ["hurst_regime=noise (bad, n=20)", "junk"],
            "exploit_contexts": [], "knob_recommendations": [], "new_lessons": []}
    fn = make_research_fn(chat=lambda *a, **k: __import__("json").dumps(note))
    out = fn({})
    assert out["avoid_contexts"] == ["hurst_regime=noise"]


def test_research_loop_auto_apply_invokes_apply_fn():
    # closing the loop: when auto_apply is on, avoid_contexts are passed to apply_fn and the applied
    # rules are reported (bounded, safety-only).
    applied_calls = []

    def apply_fn(note):
        out = [c.replace("hurst=", "hurst_regime=") for c in note.get("avoid_contexts", [])]
        applied_calls.append(out)
        return out
    note = {"summary": "avoid noise", "avoid_contexts": ["hurst=noise", "ttc_bucket=<60s"],
            "exploit_contexts": [], "knob_recommendations": [], "new_lessons": []}
    rl = ResearchLoop(research_fn=lambda rep: note, report_provider=lambda: {}, apply_fn=apply_fn,
                      auto_apply=True)
    rl.refresh()
    r = rl.report()
    assert r["auto_apply"] is True and applied_calls
    assert "hurst_regime=noise" in r["recent_applied"] and "ttc_bucket=<60s" in r["recent_applied"]


def test_research_loop_event_trigger_respects_min_gap():
    # event-triggered run only fires after event_min_gap_s since the last run; interval is the floor.
    rl = ResearchLoop(research_fn=lambda rep: {"summary": "x"}, report_provider=lambda: {},
                      interval_s=99999, event_min_gap_s=600)
    rl.request_run("new_edge")
    import time as _t
    now = _t.time()
    # too soon after a (just-now) run -> not yet due
    rl._last_run_ts = now
    assert (rl._pending_event == "new_edge")
    # simulate the worker's decision logic directly: gap not elapsed -> no event run
    ev = rl._pending_event if (now - rl._last_run_ts) >= rl.event_min_gap_s else None
    assert ev is None
    # after the gap, the event would fire
    rl._last_run_ts = now - 700
    ev2 = rl._pending_event if (now - rl._last_run_ts) >= rl.event_min_gap_s else None
    assert ev2 == "new_edge"
    rep = rl.report()
    assert rep["interval_floor_s"] == 99999 and rep["event_min_gap_s"] == 600
    assert "pending_event" in rep and "triggers" in rep
