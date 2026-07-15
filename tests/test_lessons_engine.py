"""Lessons engine tests — self-improving memory writes."""

from __future__ import annotations

from pathlib import Path

from hermes.lessons_engine import (
    append_lesson,
    format_lesson_md,
    lesson_from_rejection,
    lesson_from_settlement,
    promote_lesson,
)
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
from hermes.state_io import knowledge_path, read_text


def test_lesson_from_loss_is_avoid_rule():
    stl = Settlement(
        position_id="p1",
        signal_id="s1",
        market_id="m1",
        direction=Direction.NO,
        entry_price=0.6,
        exit_price=0.0,
        size_usd=50,
        pnl_usd=-50,
        won=False,
        regime=Regime.HIGH_VOL,
        hourly_bucket=3,
        entry_mode=EntryMode.MOMENTUM,
        confidence_tier=ConfidenceTier.B,
    )
    lesson = lesson_from_settlement(stl)
    assert "AVOID:" in lesson.rule
    assert lesson.promote_to == "ALPHA_RESEARCH_SKILL"
    assert lesson.severity == "high"


def test_append_and_promote(tmp_path: Path, monkeypatch):
    # Redirect knowledge writes into tmp via monkeypatch of knowledge_path
    import hermes.state_io as sio
    import hermes.lessons_engine as le

    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "LESSONS.md").write_text("# LESSONS\n\n## Active Lessons\n", encoding="utf-8")
    (kdir / "ALPHA_RESEARCH_SKILL.md").write_text(
        "# ALPHA\n\n## Auto-Promoted Rules\n", encoding="utf-8"
    )
    (kdir / "SKILL.md").write_text("# SKILL\n\n## Auto-Promoted Rules\n", encoding="utf-8")

    monkeypatch.setattr(sio, "KNOWLEDGE", kdir)
    monkeypatch.setattr(le, "knowledge_path", lambda name: kdir / name)

    stl = Settlement(
        position_id="p1",
        signal_id="s1",
        market_id="m1",
        direction=Direction.YES,
        entry_price=0.4,
        exit_price=0.0,
        size_usd=40,
        pnl_usd=-40,
        won=False,
        regime=Regime.MEAN_REVERT,
        hourly_bucket=10,
        entry_mode=EntryMode.MEAN_REVERSION,
        confidence_tier=ConfidenceTier.A,
    )
    lesson = lesson_from_settlement(stl)
    append_lesson(lesson)
    promote_lesson(lesson)

    lessons = (kdir / "LESSONS.md").read_text(encoding="utf-8")
    alpha = (kdir / "ALPHA_RESEARCH_SKILL.md").read_text(encoding="utf-8")
    assert lesson.lesson_id in lessons
    assert "AVOID:" in lessons
    assert lesson.lesson_id in alpha


def test_rejection_lesson():
    sig = Signal(
        market_id="m1",
        slug="s",
        question="q",
        direction=Direction.NO,
        entry_mode=EntryMode.MOMENTUM,
        confidence_tier=ConfidenceTier.B,
        conviction=0.6,
        fair_value=0.7,
        market_price=0.65,
        expected_edge=0.05,
        live_ev=0.03,
        regime=Regime.TRENDING_DOWN,
        hourly_bucket=20,
    )
    report = VerificationReport(
        signal_id=sig.signal_id,
        decision=VerifierDecision.REJECT,
        rejection_reasons=["live_ev=0.0300", "bucket_below_threshold"],
        score=0.4,
    )
    lesson = lesson_from_rejection(sig, report)
    assert "REJECT pattern" in lesson.rule
    assert format_lesson_md(lesson).startswith("\n###")
