"""Verifier unit tests — the gates that protect 80%+ WR ambition."""

from __future__ import annotations

import pytest

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


@pytest.fixture(autouse=True)
def _disable_btc_scope(monkeypatch):
    """Most verifier fixtures use non-BTC-updown markets; opt out of hard scope."""
    monkeypatch.setenv("HERMES_SCOPE_BTC_UPDOWN_ONLY", "0")


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


def test_verifier_rejects_out_of_scope(monkeypatch):
    monkeypatch.setenv("HERMES_SCOPE_BTC_UPDOWN_ONLY", "1")
    report = verify_signal(
        _signal(),
        buckets=[_good_bucket()],
        state={"capital_usd": 2000, "max_drawdown_pct": 0.0, "open_exposure_usd": 0},
        lessons="",
    )
    assert report.decision == VerifierDecision.REJECT
    assert any("out_of_scope" in r for r in report.rejection_reasons)


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
    assert report.decision != VerifierDecision.PASS
    assert report.decision in (VerifierDecision.REJECT, VerifierDecision.DEFER)


def test_verifier_rejects_drawdown_breach():
    report = verify_signal(
        _signal(),
        buckets=[_good_bucket()],
        state={"capital_usd": 10_000, "max_drawdown_pct": 0.09, "open_exposure_usd": 0},
        lessons="",
    )
    assert report.decision == VerifierDecision.REJECT


def test_verifier_rejects_bad_oracle_alignment():
    report = verify_signal(
        _signal(
            market_id="mkt_btc_5m",
            slug="btc-updown-5m",
            question="Bitcoin Up or Down - 5 Minutes",
            market_series="btc_updown_5m",
            timeframe="5m",
            oracle_alignment=0.1,
            meta={"paper": True, "asset": "BTC", "oracle_return_proxy": 0.0},
        ),
        buckets=[_good_bucket()],
        state={"capital_usd": 10_000, "max_drawdown_pct": 0.0, "open_exposure_usd": 0},
        lessons="",
    )
    assert report.decision == VerifierDecision.REJECT
    assert any("oracle" in r for r in report.rejection_reasons)


def test_verifier_defers_without_bucket_history():
    report = verify_signal(
        _signal(),
        buckets=[],  # no history
        state={"capital_usd": 10_000, "max_drawdown_pct": 0.0, "open_exposure_usd": 0},
        lessons="",
    )
    assert report.decision in (VerifierDecision.DEFER, VerifierDecision.REJECT)
    assert report.decision != VerifierDecision.PASS


def test_verifier_rejects_pretrade_skip():
    report = verify_signal(
        _signal(
            pretrade_skip=True,
            pretrade_analysis_id="pta_test",
            size_pct_recommended=0.0,
            allocation_usd=0.0,
            pretrade_reasons=["live_ev below floor"],
        ),
        buckets=[_good_bucket()],
        state={"capital_usd": 2000, "max_drawdown_pct": 0.0, "open_exposure_usd": 0},
        lessons="",
    )
    assert report.decision == VerifierDecision.REJECT
    assert any("pretrade" in r for r in report.rejection_reasons)


def test_scoped_mispricing_allows_single_sleeve_hhi(monkeypatch):
    """Option D: one active 5m sleeve → HHI=1.0 must still PASS (not starve trades)."""
    from hermes.models import AllocationProposal

    monkeypatch.setenv("HERMES_SCOPE_BTC_UPDOWN_ONLY", "1")
    sig = _signal(
        market_id="mkt_btc_5m",
        slug="btc-updown-5m-1784125200",
        question="Bitcoin Up or Down - 5 Minutes",
        market_series="btc_updown_5m",
        timeframe="5m",
        direction=Direction.YES,
        entry_mode=EntryMode.MISPRICING,
        confidence_tier=ConfidenceTier.A,
        conviction=0.85,
        fair_value=0.62,
        market_price=0.40,
        expected_edge=0.18,
        live_ev=0.12,
        regime=Regime.LOW_VOL,
        hourly_bucket=14,
        size_usd_suggested=10.0,
        allocation_usd=10.0,
        allocation_weight=1.0,
        size_pct_recommended=0.005,
        entry_vwap_target=0.40,
        pre_entry_stability_ok=False,  # softened when mispricing + VWAP set
        oracle_alignment=0.7,
        oracle_source="chainlink",
        oracle_price=65000.0,
        meta={
            "paper": True,
            "asset": "BTC",
            "mispricing_active": True,
            "mispricing_conviction": 0.9,
            "mispricing_dislocation": 0.22,
            "bandit_arm": "explore",
            "bandit_size_scale": 0.5,
            "sources_agree": True,
            "oracle_return_proxy": 0.002,
        },
    )
    proposal = AllocationProposal(
        capital_usd=2000.0,
        weights={"btc_updown_5m|mispricing|low_vol|h14|5m": 1.0},
        signal_sizes_usd={sig.signal_id: 10.0},
        diversification_ratio=1.0,
        concentration_hhi=1.0,
    )
    report = verify_signal(
        sig,
        buckets=[],  # cold-start OK via mispricing evidence
        state={"capital_usd": 2000, "max_drawdown_pct": 0.0, "open_exposure_usd": 0},
        lessons="",
        proposal=proposal,
    )
    assert report.decision == VerifierDecision.PASS, report.rejection_reasons
    assert report.sized_usd > 0
    assert not any("concentration_hhi" in r for r in report.rejection_reasons)
