"""Lessons engine — self-improving memory outside the context window.

After every settlement (and every REJECT/near-miss), extract an actionable
rule, append to LESSONS.md, and promote durable rules into
ALPHA_RESEARCH_SKILL.md or SKILL.md. Retire lessons that no longer hold
when evidence flips.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from hermes.decorators import loop
from hermes.models import (
    Lesson,
    Settlement,
    Signal,
    VerificationReport,
    VerifierDecision,
)
from hermes.state_io import (
    append_text,
    ensure_dirs,
    knowledge_path,
    read_alpha_skill,
    read_lessons_md,
    read_skill,
    read_text,
    write_text,
)

logger = logging.getLogger(__name__)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def lesson_from_settlement(stl: Settlement) -> Lesson:
    if stl.won:
        rule = (
            f"EXPLOIT continues: {stl.entry_mode.value} / {stl.regime.value} / "
            f"h{stl.hourly_bucket} / {stl.confidence_tier.value} produced a win "
            f"(pnl=${stl.pnl_usd:.2f}). Keep sizing rules; do not loosen EV gate."
        )
        severity = "low"
        promote = None
    else:
        rule = (
            f"AVOID:{stl.entry_mode.value} in {stl.regime.value} at hour={stl.hourly_bucket} "
            f"when tier={stl.confidence_tier.value} until bucket WR recovers above 65%. "
            f"Loss pnl=${stl.pnl_usd:.2f} entry={stl.entry_price:.3f} exit={stl.exit_price:.3f}."
        )
        severity = "high"
        promote = "ALPHA_RESEARCH_SKILL"
    return Lesson(
        source="settlement",
        severity=severity,
        rule=rule,
        evidence=(
            f"signal={stl.signal_id} market={stl.market_id} won={stl.won} "
            f"mode={stl.entry_mode.value} regime={stl.regime.value}"
        ),
        applies_to=[
            stl.entry_mode.value,
            stl.regime.value,
            f"h{stl.hourly_bucket}",
            stl.confidence_tier.value,
        ],
        promote_to=promote,
    )


def lesson_from_rejection(
    signal: Signal,
    report: VerificationReport,
) -> Lesson:
    reasons = ", ".join(report.rejection_reasons[:5]) or "unspecified"
    rule = (
        f"REJECT pattern: {signal.entry_mode.value}/{signal.regime.value}/"
        f"h{signal.hourly_bucket} failed verifier ({reasons}). "
        f"Do not re-propose without new evidence. live_ev was {signal.live_ev:.4f}."
    )
    return Lesson(
        source="rejection",
        severity="medium",
        rule=rule,
        evidence=f"signal={signal.signal_id} score={report.score} decision={report.decision.value}",
        applies_to=[
            signal.entry_mode.value,
            signal.regime.value,
            f"h{signal.hourly_bucket}",
        ],
        promote_to="ALPHA_RESEARCH_SKILL"
        if "bucket" in reasons or "avoid" in reasons.lower()
        else None,
    )


def format_lesson_md(lesson: Lesson) -> str:
    return (
        f"\n### [{_stamp()}] `{lesson.lesson_id}` — {lesson.severity.upper()} "
        f"({lesson.source})\n"
        f"- **Rule**: {lesson.rule}\n"
        f"- **Evidence**: {lesson.evidence}\n"
        f"- **Applies to**: {', '.join(lesson.applies_to) or 'general'}\n"
        f"- **Promote to**: {lesson.promote_to or 'none'}\n"
        f"- **Retired**: {str(lesson.retired).lower()}\n"
    )


def append_lesson(lesson: Lesson) -> None:
    ensure_dirs()
    path = knowledge_path("LESSONS.md")
    if not path.exists():
        write_text(
            path,
            "# LESSONS.md\n\nSelf-improving memory. Every loss, rejection, or "
            "near-miss adds a dated, actionable rule.\n\n## Active Lessons\n",
        )
    append_text(path, format_lesson_md(lesson))
    logger.info("lesson written: %s (%s)", lesson.lesson_id, lesson.severity)


def promote_lesson(lesson: Lesson) -> None:
    """Lift durable rules into ALPHA_RESEARCH_SKILL.md or SKILL.md."""
    if not lesson.promote_to:
        return
    target = (
        knowledge_path("ALPHA_RESEARCH_SKILL.md")
        if "ALPHA" in lesson.promote_to.upper()
        else knowledge_path("SKILL.md")
    )
    text = read_text(target)
    marker = "## Auto-Promoted Rules"
    block = (
        f"\n- [{_stamp()}] {lesson.rule} "
        f"<!-- lesson:{lesson.lesson_id} -->\n"
    )
    if marker not in text:
        text = text.rstrip() + f"\n\n{marker}\n{block}"
    else:
        text = text.replace(marker, marker + block, 1)
    write_text(target, text)
    logger.info("promoted lesson %s → %s", lesson.lesson_id, target.name)


def retire_lessons_with_evidence(
    *,
    mode: str,
    regime: str,
    new_wr: float,
    min_n: int,
) -> int:
    """Retire AVOID lessons when bucket recovers (WR>=65%, n>=min_n)."""
    if new_wr < 0.65 or min_n < 20:
        return 0
    path = knowledge_path("LESSONS.md")
    text = read_lessons_md()
    if not text:
        return 0
    # Mark matching AVOID lines as retired
    pattern = re.compile(
        rf"(### \[.+?\].*?\n(?:- \*\*.*?\n)+?)(- \*\*Retired\*\*: false)",
        re.MULTILINE,
    )
    retired = 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _maybe_retire(match: re.Match[str]) -> str:
        nonlocal retired
        block = match.group(0)
        if f"AVOID:{mode}" in block and regime in block and "Retired**: false" in block:
            retired += 1
            return block.replace(
                "- **Retired**: false",
                f"- **Retired**: true\n- **Retire evidence**: bucket recovered "
                f"WR={new_wr:.2%} n>={min_n} at {stamp}",
            )
        return block

    new_text = pattern.sub(_maybe_retire, text)
    if retired:
        write_text(path, new_text)
        logger.info("retired %d lessons for %s/%s", retired, mode, regime)
    return retired


def process_settlement(stl: Settlement) -> Lesson:
    lesson = lesson_from_settlement(stl)
    append_lesson(lesson)
    promote_lesson(lesson)
    return lesson


def process_rejection(signal: Signal, report: VerificationReport) -> Optional[Lesson]:
    if report.decision == VerifierDecision.PASS:
        return None
    # DEFER goes to human inbox — don't flood LESSONS.md every turn
    if report.decision == VerifierDecision.DEFER:
        return None
    # Deduplicate noise: only persist high-signal rejection lessons
    interesting = {
        "bucket_below_threshold",
        "lane:gated",
        "lane:killed",
        "entry_quality",
    }
    reasons = set(report.rejection_reasons)
    if not (reasons & interesting) and not any(
        r.startswith("AVOID:") or r.startswith("avoid:") or "osmani" in r.lower()
        for r in report.rejection_reasons
    ):
        # Cap: write live_ev lessons only for tier A/B near-misses
        if not any(r.startswith("live_ev") for r in report.rejection_reasons):
            return None
        if signal.confidence_tier.value not in ("A", "B"):
            return None
    lesson = lesson_from_rejection(signal, report)
    append_lesson(lesson)
    promote_lesson(lesson)
    return lesson


@loop(interval="5m", name="lessons_engine")
def lessons_engine_tick(
    settlements: Optional[list[Settlement]] = None,
    signals: Optional[list[Signal]] = None,
    reports: Optional[list[VerificationReport]] = None,
) -> list[Lesson]:
    """Persist lessons from settlements + rejections this turn."""
    ensure_dirs()
    out: list[Lesson] = []
    for stl in settlements or []:
        out.append(process_settlement(stl))

    if signals and reports:
        by_id = {s.signal_id: s for s in signals}
        for r in reports:
            if r.decision == VerifierDecision.PASS:
                continue
            sig = by_id.get(r.signal_id)
            if sig:
                les = process_rejection(sig, r)
                if les:
                    out.append(les)

    # Keep skill layer warm in logs
    _ = read_skill(), read_alpha_skill()
    logger.info("lessons_engine: wrote %d lessons", len(out))
    return out
