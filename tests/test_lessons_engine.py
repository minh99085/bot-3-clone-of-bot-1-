"""Lesson engine — scoped AVOID/REDUCE, no fleet-wide blocks on single loss."""

from __future__ import annotations

from hermes.lessons_engine import lesson_from_settlement, process_rejection
from hermes.models import (
    ConfidenceTier,
    Direction,
    EntryMode,
    Regime,
    Settlement,
    Signal,
    VerificationReport,
    VerifierDecision,
)


def _stl(**kw) -> Settlement:
    base = dict(
        position_id="pos1",
        signal_id="sig1",
        market_id="m1",
        direction=Direction.UP,
        entry_price=0.4,
        exit_price=0.0,
        size_usd=60.0,
        pnl_usd=-60.0,
        won=False,
        regime=Regime.LOW_VOL,
        hourly_bucket=21,
        entry_mode=EntryMode.MISPRICING,
        confidence_tier=ConfidenceTier.B,
        market_series="btc_updown_5m",
        substrategy_id="btc_updown_5m|mispricing|low_vol|h21|5m",
        slug="btc-updown-5m-1",
        timeframe="5m",
        paper=True,
        notes="settle_cex asset=BTC",
    )
    base.update(kw)
    return Settlement(**base)


def test_single_loss_writes_reduce_not_avoid(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INSTANCE_ID", "btc5")
    monkeypatch.setenv("HERMES_PAPER_DIR", str(tmp_path / "paper" / "btc5"))
    from hermes.state_io import ensure_dirs

    ensure_dirs()
    lesson = lesson_from_settlement(_stl())
    assert lesson.rule.startswith("REDUCE:")
    assert "AVOID:mispricing" not in lesson.rule
    assert "`btc_updown_5m`" in lesson.rule


def test_process_rejection_skips_allocation_echo():
    sig = Signal(
        market_id="m1",
        slug="btc-updown-5m-1",
        question="q",
        direction=Direction.UP,
        entry_mode=EntryMode.MISPRICING,
        confidence_tier=ConfidenceTier.A,
        conviction=0.9,
        fair_value=0.6,
        market_price=0.4,
        expected_edge=0.2,
        live_ev=0.1,
        regime=Regime.LOW_VOL,
        hourly_bucket=21,
        market_series="btc_updown_5m",
    )
    report = VerificationReport(
        signal_id=sig.signal_id,
        decision=VerifierDecision.REJECT,
        checks=[],
        score=0.76,
        rejection_reasons=[
            "avoid:mispricing",
            "pretrade_skip: lesson_AVOID",
            "allocation:zero_allocation_weight",
        ],
        sized_usd=0.0,
    )
    assert process_rejection(sig, report) is None
