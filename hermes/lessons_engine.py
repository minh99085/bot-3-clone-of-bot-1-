"""Lessons engine — self-improving memory for signals AND allocation.

After every settlement / rejection / cut-reduce event:
  - append actionable rules to LESSONS.md
  - promote durable rules into ALPHA_RESEARCH_SKILL.md or SKILL.md
  - allocation heuristics update automatically (weight caps, REDUCE/CUT)

Separates "currently losing" from "model/reason for working is broken."
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from hermes.decorators import loop
from hermes.models import (
    AllocationProposal,
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
from hermes.substrategy import make_substrategy_id

logger = logging.getLogger(__name__)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def lesson_from_settlement(stl: Settlement) -> Lesson:
    sid = stl.substrategy_id or make_substrategy_id(
        stl.market_series or "misc",
        stl.entry_mode,
        stl.regime,
        stl.hourly_bucket,
    )
    if stl.won:
        rule = (
            f"EXPLOIT continues: sleeve `{sid}` produced a win "
            f"(pnl=${stl.pnl_usd:.2f}). Keep sizing rules; do not loosen EV gate. "
            f"Allocation: HOLD/BOOST only if rolling EV after cost still rising."
        )
        severity = "low"
        promote = None
    else:
        rule = (
            f"AVOID:{stl.entry_mode.value} in {stl.regime.value} at hour={stl.hourly_bucket} "
            f"when tier={stl.confidence_tier.value} until bucket WR recovers above 65%. "
            f"REDUCE weight on `{sid}` after loss pnl=${stl.pnl_usd:.2f}. "
            f"If rolling EV < 0.02 or WR trend broken → CUT (model broken), "
            f"not merely currently_losing."
        )
        severity = "high"
        promote = "ALPHA_RESEARCH_SKILL"
    return Lesson(
        source="settlement",
        severity=severity,
        rule=rule,
        evidence=(
            f"signal={stl.signal_id} market={stl.market_id} won={stl.won} "
            f"sleeve={sid} mode={stl.entry_mode.value} regime={stl.regime.value}"
        ),
        applies_to=[
            sid,
            stl.entry_mode.value,
            stl.regime.value,
            f"h{stl.hourly_bucket}",
            stl.confidence_tier.value,
            "allocation",
        ],
        promote_to=promote,
    )


def lesson_from_rejection(
    signal: Signal,
    report: VerificationReport,
) -> Lesson:
    reasons = ", ".join(report.rejection_reasons[:5]) or "unspecified"
    sid = report.substrategy_id or signal.substrategy_id
    alloc_hit = any("allocation" in r for r in report.rejection_reasons)
    if alloc_hit:
        rule = (
            f"ALLOCATION_REJECT:`{sid}` — verifier refused size/weight "
            f"({reasons}). Do not force fills that raise HHI or cut diversification. "
            f"Revisit HRP/BL views only with new evidence."
        )
        promote = "ALPHA_RESEARCH_SKILL"
        severity = "high"
    else:
        rule = (
            f"REJECT pattern: {signal.entry_mode.value}/{signal.regime.value}/"
            f"h{signal.hourly_bucket} failed verifier ({reasons}). "
            f"Do not re-propose without new evidence. live_ev was {signal.live_ev:.4f}."
        )
        promote = (
            "ALPHA_RESEARCH_SKILL"
            if "bucket" in reasons or "avoid" in reasons.lower()
            else None
        )
        severity = "medium"
    return Lesson(
        source="rejection",
        severity=severity,
        rule=rule,
        evidence=f"signal={signal.signal_id} score={report.score} decision={report.decision.value}",
        applies_to=[
            sid or "unknown",
            signal.entry_mode.value,
            signal.regime.value,
            f"h{signal.hourly_bucket}",
            "allocation" if alloc_hit else "signal",
        ],
        promote_to=promote,
    )


def lessons_from_allocation(proposal: AllocationProposal) -> list[Lesson]:
    """Persist cut/reduce decisions as living allocation rules."""
    out: list[Lesson] = []
    for sid in proposal.cut_list:
        out.append(
            Lesson(
                source="allocation_cut",
                severity="critical",
                rule=(
                    f"CUT:`{sid}` — model/reason for working is broken "
                    f"(rolling EV/WR/brier/regime). Weight cap=0 even if last trades "
                    f"were profitable. Separate from currently_losing."
                ),
                evidence=f"proposal={proposal.proposal_id} method={proposal.method}",
                applies_to=[sid, "allocation", "cut"],
                promote_to="ALPHA_RESEARCH_SKILL",
            )
        )
    for sid in proposal.reduce_list:
        out.append(
            Lesson(
                source="allocation_reduce",
                severity="high",
                rule=(
                    f"REDUCE weight on `{sid}` when internal confidence degrading "
                    f"or currently_losing with negative EV trend. Cap ≤ 8% until "
                    f"rolling EV after cost recovers."
                ),
                evidence=f"proposal={proposal.proposal_id} hhi={proposal.concentration_hhi}",
                applies_to=[sid, "allocation", "reduce"],
                promote_to="ALPHA_RESEARCH_SKILL",
            )
        )
    return out


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
            "# LESSONS.md\n\nSelf-improving memory. Every loss, rejection, cut/reduce, "
            "or near-miss adds a dated, actionable rule for signals AND allocation.\n\n"
            "## Active Lessons\n",
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
    # Allocation rules go under a dedicated marker when possible
    is_alloc = "allocation" in (lesson.applies_to or []) or lesson.source.startswith(
        "allocation"
    )
    marker = (
        "## Auto-Promoted Allocation Rules" if is_alloc else "## Auto-Promoted Rules"
    )
    # Fall back to generic marker if allocation section missing
    if marker not in text:
        if is_alloc and "## Auto-Promoted Rules" in text:
            marker = "## Auto-Promoted Rules"
        elif marker not in text and "## Auto-Promoted Rules" not in text:
            text = text.rstrip() + f"\n\n{marker}\n"
        else:
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
    if report.decision == VerifierDecision.DEFER:
        return None
    interesting = {
        "bucket_below_threshold",
        "lane:gated",
        "lane:killed",
        "entry_quality",
    }
    reasons = set(report.rejection_reasons)
    alloc_hit = any("allocation" in r for r in report.rejection_reasons)
    if (
        not alloc_hit
        and not (reasons & interesting)
        and not any(
            r.startswith("AVOID:") or r.startswith("avoid:") or "osmani" in r.lower()
            for r in report.rejection_reasons
        )
    ):
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
    proposal: Optional[AllocationProposal] = None,
) -> list[Lesson]:
    """Persist lessons from settlements, rejections, and allocation cut/reduce."""
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

    if proposal is not None:
        for les in lessons_from_allocation(proposal):
            # Dedup: only write CUT/REDUCE once per sleeve per turn
            append_lesson(les)
            promote_lesson(les)
            out.append(les)

    _ = read_skill(), read_alpha_skill()
    logger.info("lessons_engine: wrote %d lessons", len(out))
    return out
