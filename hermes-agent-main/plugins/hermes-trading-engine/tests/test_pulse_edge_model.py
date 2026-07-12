"""Calibrated edge model (Phase 7) — scaffolding-first, leakage + min-sample guards, no authority."""

from __future__ import annotations

from engine.pulse.edge_model import (EdgeModel, extract_features, calibration_bucket,
                                      FEATURE_NAMES, LABEL_FIELDS)


def test_min_sample_guard_returns_scaffolding():
    m = EdgeModel(min_samples=50)
    vec = {f: 0.1 for f in FEATURE_NAMES}
    out = m.predict(vec)
    assert out["trained"] is False and out["reason"] == "insufficient_labeled_samples"
    assert out["p_up"] is None and out["model_confidence"] is None


def test_no_leakage_label_fields_not_features():
    # the label/outcome fields must never be part of the model's feature set
    assert not (set(LABEL_FIELDS) & set(FEATURE_NAMES))
    feats = {"hurst": 0.6, "autocorr_lag1": 0.1, "realized_vol": 5e-5, "zscore": 1.0}
    vec = extract_features(features=feats, signals={"direction": "up", "strength": 0.5},
                           factors={"edge_quality_score": 0.7, "orderbook_imbalance": 0.2})
    assert set(vec) == set(FEATURE_NAMES)               # only entry-time features
    assert "outcome_up" not in vec and "won" not in vec
    m = EdgeModel(min_samples=10)
    r = m.report()
    assert r["has_trade_authority"] is False and "entry_features_only" in r["leakage_guard"]


def test_model_trains_and_predicts_learnable_signal():
    # learnable: outcome_up == (signal_strength_signed > 0); model should separate after training
    m = EdgeModel(min_samples=40, lr=0.2)
    import random
    rng = random.Random(3)
    for _ in range(400):
        signed = rng.uniform(-1, 1)
        vec = {f: 0.0 for f in FEATURE_NAMES}
        vec["signal_strength_signed"] = signed
        m.observe_label(vec, outcome_up=(signed > 0))
    assert m.trained is True
    up_vec = {f: 0.0 for f in FEATURE_NAMES}; up_vec["signal_strength_signed"] = 0.9
    dn_vec = {f: 0.0 for f in FEATURE_NAMES}; dn_vec["signal_strength_signed"] = -0.9
    assert m.predict(up_vec)["p_up"] > 0.6 and m.predict(dn_vec)["p_up"] < 0.4
    rep = m.report()
    assert rep["observe_only"] is True and rep["trained"] is True
    assert rep["calibration_table"]                     # buckets populated


def test_calibration_bucket():
    assert calibration_bucket(0.05) == "0.0-0.1" and calibration_bucket(0.95) == "0.9-1.0"
    assert calibration_bucket(None) == "na"
