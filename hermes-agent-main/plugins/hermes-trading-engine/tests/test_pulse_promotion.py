"""Phase 12 promotion ladder — default observe-only; no feature exceeds authorized level."""

from __future__ import annotations

from engine.pulse.promotion import (PromotionLadder, can_promote, AUTHORITY_LEVELS, MAX_LEVEL,
                                     DEFAULT_FEATURES)


def test_all_features_default_observe_only():
    L = PromotionLadder()
    assert all(L.effective_authority(f) == 0 for f in DEFAULT_FEATURES)
    assert L.max_authority() == 0
    r = L.report()
    assert r["all_observe_only"] is True and r["max_authority_in_use"] == 0
    assert AUTHORITY_LEVELS[0] == "observe_only"


def test_promotion_refused_without_all_gates():
    L = PromotionLadder(min_samples=200)
    # no config flag -> refused
    out = L.promote("edge_model", 2, config_flag=False, samples=500, ev_after_costs=0.1,
                    reconciled=True, report_evidence=True)
    assert out["promoted"] is False and "config_flag_not_set" in out["reasons"]
    assert L.effective_authority("edge_model") == 0
    # insufficient samples + non-positive EV + unclean reconciliation -> refused with reasons
    out2 = L.promote("edge_model", 2, config_flag=True, samples=10, ev_after_costs=-0.01,
                     reconciled=False, report_evidence=False)
    assert out2["promoted"] is False
    for r in ("insufficient_samples", "ev_not_positive", "reconciliation_unclean",
              "no_report_evidence"):
        assert r in out2["reasons"]
    assert L.max_authority() == 0


def test_promotion_allowed_only_with_all_gates():
    L = PromotionLadder(min_samples=200)
    out = L.promote("edge_model", 2, config_flag=True, samples=500, ev_after_costs=0.1,
                    reconciled=True, report_evidence=True)
    assert out["promoted"] is True and out["level"] == 2
    assert L.effective_authority("edge_model") == 2
    # other features remain observe-only -> no feature exceeds its authorized level
    assert L.effective_authority("signal_engine") == 0


def test_invalid_level_and_unknown_feature():
    L = PromotionLadder()
    assert L.promote("edge_model", MAX_LEVEL + 1, config_flag=True, samples=999,
                     ev_after_costs=1.0, reconciled=True, report_evidence=True)["reasons"] \
        == ["invalid_level"]
    assert L.promote("nope", 1, config_flag=True, samples=999, ev_after_costs=1.0,
                     reconciled=True, report_evidence=True)["reasons"] == ["unknown_feature"]


def test_can_promote_pure_gate():
    ok, reasons = can_promote(config_flag=True, samples=300, min_samples=200,
                              ev_after_costs=0.05, reconciled=True, report_evidence=True)
    assert ok is True and reasons == []
