"""Hermes v2 — main orchestrator.

Wires the five moves into one turn:
  Discovery → Handoff → Verification → Persistence → Scheduling

Automations (@loop) provide cadence; @goal provides verifiable stop conditions.
Risk monitor runs on its own cadence in a parallel worktree.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes.decorators import get_checkers, get_goals, get_loops, goal, loop, run_loop_forever
from hermes.discovery import discovery_tick
from hermes.executor import executor_tick
from hermes.lessons_engine import lessons_engine_tick
from hermes.models import LoopTurnResult, Settlement, VerifierDecision, new_id
from hermes.portfolio import allocation_handoff
from hermes.risk_monitor import risk_monitor_tick
from hermes.signal_generator import signal_generator_tick
from hermes.state_io import (
    append_jsonl,
    ensure_dirs,
    ledger_path,
    parse_state_fields,
    read_state_md,
    update_state_field,
    write_handoff,
)
from hermes.verifier import verifier_tick
from hermes.worktrees import ensure_worktree

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("hermes.loop")

ROOT = Path(__file__).resolve().parents[1]


def _goal_high_conviction_done(result: LoopTurnResult, *args, **kwargs) -> bool:
    """External checker for @goal — never let the maker decide 'done'."""
    if result.signals_passed >= 3:
        return True
    if result.paused:
        return True
    return False


@goal(
    condition="3+ verified high-edge signals OR pause/48h pass with no action",
    checker=_goal_high_conviction_done,
    max_turns=24,
    max_hours=48.0,
    description="Keep iterating until we have 3+ verified signals or the loop pauses",
)
def high_conviction_session(paper: bool = True) -> LoopTurnResult:
    """Example /goal pattern for a single market research burst."""
    return run_one_turn(paper=paper)


@loop(interval="5m", name="hermes_main")
def run_one_turn(paper: bool = True, turn_id: Optional[str] = None) -> LoopTurnResult:
    """One full turn of the Financial Freedom Bot loop."""
    ensure_dirs()
    ensure_worktree("research")
    ensure_worktree("signal")
    # risk worktree is owned by risk_monitor

    tid = turn_id or new_id("trn_")
    result = LoopTurnResult(turn_id=tid, started_at=datetime.now(timezone.utc))

    state = parse_state_fields(read_state_md())
    if state.get("pause_loop") or state.get("loop_paused"):
        result.paused = True
        result.pause_reason = str(state.get("pause_reason", "STATE.md pause flag"))
        result.finished_at = datetime.now(timezone.utc)
        result.summary = f"PAUSED: {result.pause_reason}"
        logger.warning(result.summary)
        return result

    # Pre-flight risk snapshot (non-blocking read; dedicated loop also runs)
    snap = risk_monitor_tick(paper=paper)
    if snap.pause_loop:
        result.paused = True
        result.pause_reason = snap.trip_reason
        result.finished_at = datetime.now(timezone.utc)
        result.summary = f"RISK PAUSE: {snap.trip_reason}"
        return result

    # 1. Discovery
    candidates = discovery_tick(turn_id=tid)
    result.candidates_found = len(candidates)

    # 2. Signal generation (alpha research skill)
    signals = signal_generator_tick(candidates=candidates, turn_id=tid, paper=paper)
    result.signals_generated = len(signals)

    # 2b. Handoff — portfolio allocation (HRP + Ledoit-Wolf + BL views)
    proposal, signals = allocation_handoff(signals, turn_id=tid, paper=paper)

    # 3. Verification (maker-checker — signal AND allocation)
    reports = verifier_tick(signals=signals, turn_id=tid, proposal=proposal)
    result.signals_passed = sum(1 for r in reports if r.decision == VerifierDecision.PASS)
    result.signals_rejected = sum(
        1 for r in reports if r.decision == VerifierDecision.REJECT
    )
    result.deferred_to_inbox = sum(
        1 for r in reports if r.decision == VerifierDecision.DEFER
    )

    # 4. Execution (PASS only)
    fills = executor_tick(signals=signals, reports=reports, turn_id=tid)
    result.orders_sent = len(fills)

    # 5. Persistence — lessons from rejections + allocation (settlements arrive async)
    lessons = lessons_engine_tick(
        signals=signals, reports=reports, proposal=proposal
    )
    result.lessons_written = len(lessons)

    # Update STATE.md snapshot
    update_state_field("Last Turn", tid)
    update_state_field(
        "Last Turn Summary",
        f"{result.candidates_found} cand / {result.signals_generated} sig / "
        f"{result.signals_passed} pass / {result.orders_sent} fills / "
        f"div={proposal.diversification_ratio:.2f}",
    )
    update_state_field(
        "Last Turn At",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    result.finished_at = datetime.now(timezone.utc)
    result.summary = (
        f"turn={tid} candidates={result.candidates_found} "
        f"signals={result.signals_generated} pass={result.signals_passed} "
        f"reject={result.signals_rejected} fills={result.orders_sent} "
        f"lessons={result.lessons_written}"
    )
    append_jsonl(ledger_path(paper=paper).parent / "turns.jsonl", result)
    write_handoff("turn_result", [result], tid)
    logger.info(result.summary)
    return result


def simulate_settlement_for_demo(paper: bool = True) -> list[Settlement]:
    """Optional demo helper: settle open paper positions with a coin-flip-ish model.

    Real deployments should call this from a resolution connector webhook.
    """
    from hermes.models import (
        ConfidenceTier,
        Direction,
        EntryMode,
        Regime,
    )

    # Demo: one synthetic settlement so lessons_engine can show a full cycle
    stl = Settlement(
        position_id="pos_demo",
        signal_id="sig_demo",
        market_id="mkt_fed_cut",
        direction=Direction.NO,
        entry_price=0.69,
        exit_price=1.0,
        size_usd=100.0,
        pnl_usd=44.9,
        won=True,
        regime=Regime.MEAN_REVERT,
        hourly_bucket=14,
        entry_mode=EntryMode.MEAN_REVERSION,
        confidence_tier=ConfidenceTier.A,
        market_series="macro_rates",
        substrategy_id="macro_rates|mean_reversion|mean_revert|h14",
        paper=paper,
        notes="demo settlement",
    )
    append_jsonl(ledger_path(paper=paper), {"event": "settlement", **stl.model_dump(mode="json")})
    lessons_engine_tick(settlements=[stl])
    return [stl]


async def run_overnight(paper: bool = True, main_interval: float = 300.0) -> None:
    """Schedule main loop + risk monitor until cancelled."""
    ensure_dirs()
    stop = asyncio.Event()

    async def main_cadence() -> None:
        while not stop.is_set():
            await asyncio.to_thread(run_one_turn, paper)
            try:
                await asyncio.wait_for(stop.wait(), timeout=main_interval)
            except asyncio.TimeoutError:
                continue

    async def risk_cadence() -> None:
        while not stop.is_set():
            await asyncio.to_thread(risk_monitor_tick, paper)
            try:
                await asyncio.wait_for(stop.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                continue

    logger.info(
        "Hermes overnight start paper=%s loops=%s checkers=%s goals=%s",
        paper,
        list(get_loops()),
        list(get_checkers()),
        list(get_goals()),
    )
    try:
        await asyncio.gather(main_cadence(), risk_cadence())
    except asyncio.CancelledError:
        stop.set()
        raise


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Financial Freedom Bot — Hermes v2 loop orchestrator"
    )
    parser.add_argument(
        "command",
        choices=["once", "overnight", "goal", "demo", "status"],
        help="once=single turn; overnight=cadence; goal=high-conviction session; demo=full cycle; status=registry",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (also requires HERMES_LIVE=1 and STATE live_enabled)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=300.0,
        help="Main loop interval seconds for overnight mode (default 300)",
    )
    args = parser.parse_args(argv)
    paper = not args.live

    if args.command == "status":
        print("loops:", get_loops())
        print("checkers:", get_checkers())
        print("goals:", get_goals())
        return 0
    if args.command == "once":
        result = run_one_turn(paper=paper)
        print(result.summary)
        return 0
    if args.command == "goal":
        result = high_conviction_session(paper=paper)
        print(result.summary)
        return 0
    if args.command == "demo":
        import os

        os.environ["HERMES_FORCE_SYNTHETIC"] = "1"
        result = run_one_turn(paper=True)
        simulate_settlement_for_demo(paper=True)
        print(result.summary)
        print("demo settlement + lesson written")
        return 0
    if args.command == "overnight":
        asyncio.run(run_overnight(paper=paper, main_interval=args.interval))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
