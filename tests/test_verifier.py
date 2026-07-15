"""Verifier unit tests — the gates that protect 80%+ WR ambition."""

from __future__ import annotations

from hermes.models import (
    ConfidenceTier,
    Direction,
    EdgeBucket,
    EntryMode,
    Regime,
    Signal,
    VerifierDecision,
)
from hermes.verifier import verify_signal


def _signal(**overrides) -> Signal:
    base = dict(
        market_id="mkt_fed_cut",
        slug="fed-rate-cut-july",
        question="Will the Fed cut rates in July?",
        direction=Direction.NO,
        entry_mode=EntryMode.MEAN_REVERSION,
        confidence_tier=ConfidenceTier.A,
        conviction=0.8,
        fair_value=0.75,
        market_price=0.69,
        expected_edge=0.09,
        live_ev=0.075,
        regime=Regime.MEAN_REVERT,
        hourly_bucket=14,
        size_usd_suggested=100.0,
        entry_vwap_target=0.695,
        pre_entry_stability_ok=True,
        avoid_bucket_hit=False,
        meta={"paper": True},
    )
    base.update(overrides)
    return Signal(**base)


def _good_bucket() -> EdgeBucket:
    return EdgeBucket(
        regime=Regime.MEAN_REVERT,
        hourly_bucket=14,
        entry_mode=EntryMode.MEAN_REVERSION,
        confidence_tier=ConfidenceTier.A,
        sample_n=48,
        win_rate=0.78,
        avg_edge=0.09,
        profit_factor=1.9,
        max_drawdown=0.05,
        exploit=True,
    )


def test_verifier_passes_clean_signal():
    report = verify_signal(
        _signal(),
        buckets=[_good_bucket()],
        state={"capital_usd": 10_000, "max_drawdown_pct": 0.01, "open_exposure_usd": 0},
        lessons="# LESSONS\n",
    )
    assert report.decision == VerifierDecision.PASS
    assert report.sized_usd > 0
    assert all(c.passed for c in report.checks)


def test_verifier_rejects_low_ev():
    report = verify_signal(
        _signal(live_ev=0.02),
        buckets=[_good_bucket()],
        state={"capital_usd": 10_000, "max_drawdown_pct": 0.0, "open_exposure_usd": 0},
        lessons="",
    )
    assert report.decision == VerifierDecision.REJECT
    assert any("live_ev" in r for r in report.rejection_reasons)


def test_verifier_rejects_tier_c():
    report = verify_signal(
        _signal(confidence_tier=ConfidenceTier.C, conviction=0.4),
        buckets=[_good_bucket()],
        state={"capital_usd": 10_000, "max_drawdown_pct": 0.0, "open_exposure_usd": 0},
        lessons="",
    )
    assert report.decision == VerifierDecision.REJECT


def test_verifier_rejects_osmani_lane():
    report = verify_signal(
        _signal(entry_mode=EntryMode.OSMANI_LANE),
        buckets=[_good_bucket()],
        state={"capital_usd": 10_000, "max_drawdown_pct": 0.0, "open_exposure_usd": 0},
        lessons="AVOID:osmani_lane",
    )
    assert report.decision == VerifierDecision.REJECT


def test_verifier_rejects_drawdown_breach():
    report = verify_signal(
        _signal(),
        buckets=[_good_bucket()],
        state={"capital_usd": 10_000, "max_drawdown_pct": 0.09, "open_exposure_usd": 0},
        lessons="",
    )
    assert report.decision == VerifierDecision.REJECT


def test_verifier_defers_without_bucket_history():
    report = verify_signal(
        _signal(),
        buckets=[],  # no history
        state={"capital_usd": 10_000, "max_drawdown_pct": 0.0, "open_exposure_usd": 0},
        lessons="",
    )
    assert report.decision in (VerifierDecision.DEFER, VerifierDecision.REJECT)
    assert report.decision != VerifierDecision.PASS
