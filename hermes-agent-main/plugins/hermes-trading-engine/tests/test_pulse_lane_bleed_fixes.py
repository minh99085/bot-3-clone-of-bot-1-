"""Tests for directional bleed fixes (dep-arb MC veto removed)."""

from types import SimpleNamespace

from engine.pulse.engine import PulseEngine
from engine.pulse.verifier import ClaudeVerifier


def test_follow_executable_edge_blocks_coinflip_underdog():
    eng = PulseEngine.__new__(PulseEngine)
    eng.cfg = SimpleNamespace(
        council_min_executable_margin=0.06,
        min_edge=0.004,
        edge_buffer=0.01,
    )
    ok, _ = eng._follow_executable_edge_ok(p_win=0.53, ask=0.35)
    assert ok
    ok, reason = eng._follow_executable_edge_ok(p_win=0.53, ask=0.50)
    assert not ok
    assert reason == "follow_executable_margin_low"


def test_verifier_explore_approve_only_for_exploration_flag():
    v = ClaudeVerifier(verify_fn=lambda _p: None, explore_approve=True)
    v._results["dec-1"] = {"approve": False, "reason": "coinflip", "pending": False}
    out = v.verdict_or_failopen("dec-1", exploration=False)
    assert not out.get("approve")
    out2 = v.verdict_or_failopen("dec-1", exploration=True)
    assert out2.get("approve")
    assert "explore_approve" in str(out2.get("reason", ""))
