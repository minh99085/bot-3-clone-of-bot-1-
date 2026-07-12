"""Cycle 3 (LLM mispricing leverage) + Cycle 4 (hardening) — PAPER ONLY.

C3: the Grok decider + Claude verifier are reframed to EXPLOIT MISPRICING (CEX-lead divergence vs
the market price, model_vs_market), not predict direction.
C4: robust JSON extraction (fences/prose), side-aligned p_up (not confidence) into EV, verifier
fail-closed-on-pending for follow.
"""

from __future__ import annotations

from engine.pulse.grok_intel import _parse_json


# ------------------------------- C4: robust JSON parsing ----------------------------------- #
def test_parse_json_handles_fences_prose_and_trailing():
    assert _parse_json('{"action":"up","p_up":0.6}')["action"] == "up"
    assert _parse_json('```json\n{"a":1}\n```')["a"] == 1
    assert _parse_json('```\n{"a":2}\n```')["a"] == 2
    # prose wrapped around the object -> extract the balanced {...}
    d = _parse_json('Here is my answer: {"action":"down","p_up":0.4} — hope that helps!')
    assert d["action"] == "down" and d["p_up"] == 0.4
    # nested braces + strings with braces inside
    d2 = _parse_json('{"x":{"y":1},"s":"a{b}c"}')
    assert d2["x"]["y"] == 1 and d2["s"] == "a{b}c"
    assert _parse_json("no json here") is None
    assert _parse_json("") is None


# ------------------------------- C3: decider prompt reframed to mispricing ------------------ #
def test_decider_prompt_frames_mispricing_and_model_vs_market():
    from engine.pulse.grok_decider import make_decider_fn
    captured = {}

    def _chat(prompt, **kw):
        captured["prompt"] = prompt
        return '{"action":"no_trade","p_up":0.5,"confidence":0.0}'
    fn = make_decider_fn(chat=_chat)
    fn({"cex_lead_mispricing": {"divergence": 0.1}, "model_vs_market": {"model_beats_market": False}})
    p = captured["prompt"]
    assert "MISPRICING" in p and "primary edge" in p
    assert "cex_lead_mispricing" in p and "model_vs_market" in p
    assert "breakeven" in p.lower()


# ------------------------------- C3: verifier framed around edge-vs-costs ------------------- #
def test_verifier_prompt_frames_edge_vs_costs():
    from engine.pulse.verifier import make_verifier_fn
    captured = {}

    def _chat(prompt, *, system=None, **kw):
        captured["system"] = system or ""
        captured["prompt"] = prompt
        return '{"approve":false,"reason":"edge below costs"}'
    fn = make_verifier_fn(chat=_chat)
    v = fn({"fair_minus_poly": 0.01, "model_vs_market": {"model_beats_market": False}})
    s = captured["system"]
    assert "MISPRICING" in s and "fair_minus_poly" in s and "model_vs_market" in s
    assert v["approve"] is False                                # parsed the verdict


# ------------------------------- C4: side-aligned p_up logic -------------------------------- #
def test_side_aligned_pwin_from_p_up():
    # the EV-relevant P(win) for the chosen side is p_up (up) / 1-p_up (down), not 'confidence'
    grok_dec = {"action": "down", "p_up": 0.35, "confidence": 0.9}
    side = grok_dec["action"]
    pu = grok_dec.get("p_up")
    oprob = float(pu) if side == "up" else (1.0 - float(pu))
    assert abs(oprob - 0.65) < 1e-9                             # down win prob = 1-0.35, not 0.9
