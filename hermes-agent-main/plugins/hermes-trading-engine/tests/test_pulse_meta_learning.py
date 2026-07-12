"""Phase 11 LLM batch meta-learning — bundle + structured diagnostics, never live decisions."""

from __future__ import annotations

from engine.pulse.meta_learning import (build_bundle, grok_meta_diagnose, LEARNING_QUESTIONS)


def _light_report():
    return {"candidate_lifecycle": {"created": 10, "reconciled": True},
            "execution_stats": {"candidates": 5, "accepted": 2},
            "reject_reasons": {"wide_spread": 1},
            "ev_before_after_costs": {"avg_ev_before_costs": 0.05, "avg_ev_after_costs": 0.02},
            "calibration": {"brier": 0.22}, "edge_model_calibration": {},
            "sample_sizes": {"accepted": 2}, "missing_data_reasons": {"hurst:x": 3},
            "confidence_tier_table": {"tier_census": {"A+": 0, "B": 3}},
            "promotion_candidates": [], "demotion_candidates": ["edge_quality:low"],
            "pnl_by_hurst_regime": {"trending": {"n": 2, "win_rate": 1.0, "pnl_usd": 5.0}}}


def test_bundle_is_compact_and_has_learning_questions():
    b = build_bundle(_light_report())
    assert b["no_live_trading_decisions"] is True and b["report_only"] is True
    assert b["learning_questions"] == LEARNING_QUESTIONS
    assert "pnl_by_hurst_regime" in b["bucket_pnl"]
    assert b["tier_census"] == {"A+": 0, "B": 3}
    assert b["execution_stats"]["accepted"] == 2


def test_diagnose_missing_integration():
    out = grok_meta_diagnose(build_bundle(_light_report()), assessor=None,
                             integration_available=False)
    assert out["integration"] == "missing" and out["diagnostic"] is None
    assert out["no_live_trading_decisions"] is True


def test_diagnose_structured_and_strips_trade_keys():
    def fake(prompt, bundle_json):
        # a well-behaved assessor returns diagnostics; we also include a forbidden trade key
        return {"observations": ["wide_spread dominates rejects"],
                "hypotheses": ["thin books late in window"],
                "suggested_experiments": ["tighten max_spread"],
                "data_gaps": ["need more settled samples"],
                "buy_now": True, "size_usd": 100}   # must be stripped
    out = grok_meta_diagnose(build_bundle(_light_report()), assessor=fake,
                             integration_available=True)
    assert out["integration"] == "available" and out["reason"] == "ok"
    assert out["no_live_trading_decisions"] is True
    d = out["diagnostic"]
    assert set(d) == {"observations", "hypotheses", "suggested_experiments", "data_gaps"}
    assert "buy_now" not in d and "size_usd" not in d      # trade-like keys stripped


def test_diagnose_handles_bad_assessor():
    out = grok_meta_diagnose(build_bundle(_light_report()), assessor=lambda p, b: None,
                             integration_available=True)
    assert out["diagnostic"] is None and out["reason"] == "no_structured_response"
