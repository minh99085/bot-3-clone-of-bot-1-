"""WS2 — verifier veto-quality grading + exploration approve-path (stop starving cold-start).

Proves: (1) the verifier grades its OWN vetoes — if the trades it killed would have WON, it reports
'vetoes_costing_edge', else 'good_vetoes'; (2) with explore_approve on, an exploration-tagged
proposal the verifier VETOED is downgraded to a SHRUNK approve (never an enlargement) so data can be
collected; with it off, the veto stands. PAPER ONLY, gated, default OFF.
"""

from __future__ import annotations

from engine.pulse.verifier import ClaudeVerifier


def _veto_verifier(**kw):
    v = ClaudeVerifier(verify_fn=lambda p: None, enabled=False, **kw)
    v._results["d1"] = {"approve": False, "max_size_fraction": 1.0, "confidence": 0.7,
                        "reason": "edge_not_real"}
    return v


def test_explore_approve_downgrades_veto_to_shrunk_approve():
    v = _veto_verifier(explore_approve=True, explore_max_size_fraction=0.5)
    # non-exploration: the veto stands
    assert v.verdict_or_failopen("d1")["approve"] is False
    # exploration: veto -> shrunk approve, never enlarged
    out = v.verdict_or_failopen("d1", exploration=True)
    assert out["approve"] is True and out["max_size_fraction"] <= 0.5
    assert out["verifier_vetoed"] is True and v.exploration_approvals == 1


def test_explore_approve_off_keeps_veto():
    v = _veto_verifier(explore_approve=False)
    assert v.verdict_or_failopen("d1", exploration=True)["approve"] is False
    assert v.exploration_approvals == 0


def test_veto_quality_flags_costly_vetoes():
    # 25 vetoed setups that WOULD HAVE WON -> the veto is destroying edge
    v = ClaudeVerifier(verify_fn=lambda p: None, enabled=False, veto_quality_min_n=20)
    for i in range(25):
        v._results["v%d" % i] = {"approve": False, "reason": "veto"}
        v.grade("v%d" % i, won=True, pnl=4.0, acted=False)
    rep = v.report()["veto_quality"]
    assert rep["verdict"] == "vetoes_costing_edge" and rep["n"] == 25
    assert rep["vetoed_would_have_win_rate"] == 1.0


def test_veto_quality_flags_good_vetoes():
    v = ClaudeVerifier(verify_fn=lambda p: None, enabled=False, veto_quality_min_n=20)
    for i in range(25):                                   # vetoed setups that would have LOST
        v._results["v%d" % i] = {"approve": False, "reason": "veto"}
        v.grade("v%d" % i, won=False, pnl=-5.0, acted=False)
    rep = v.report()["veto_quality"]
    assert rep["verdict"] == "good_vetoes" and rep["vetoed_would_have_pnl_usd"] < 0


def test_veto_quality_insufficient_below_min():
    v = ClaudeVerifier(verify_fn=lambda p: None, enabled=False, veto_quality_min_n=20)
    v._results["v0"] = {"approve": False, "reason": "veto"}
    v.grade("v0", won=True, pnl=4.0, acted=False)
    assert v.report()["veto_quality"]["verdict"] == "insufficient_evidence"


def test_exploration_approvals_persist():
    v = _veto_verifier(explore_approve=True)
    v.verdict_or_failopen("d1", exploration=True)
    v2 = ClaudeVerifier(verify_fn=lambda p: None, enabled=False)
    v2.load_state(v.to_state())
    assert v2.report()["exploration_approvals"] == 1
