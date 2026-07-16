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
from hermes.health import start_health_server, write_heartbeat
from hermes.lessons_engine import lessons_engine_tick
from hermes.logging_config import enforce_paper_only, is_paper_only, setup_logging
from hermes.models import LoopTurnResult, Settlement, VerifierDecision, new_id
from hermes.portfolio import allocation_handoff
from hermes.pretrade import run_pretrade_sizing
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
    enforce_paper_only()
    if is_paper_only():
        paper = True

    ensure_dirs()
    ensure_worktree("research")
    ensure_worktree("signal")
    # risk worktree is owned by risk_monitor

    tid = turn_id or new_id("trn_")
    result = LoopTurnResult(turn_id=tid, started_at=datetime.now(timezone.utc))
    write_heartbeat(last_turn=tid, summary="turn_start")

    state = parse_state_fields(read_state_md())
    if state.get("pause_loop") or state.get("loop_paused"):
        result.paused = True
        result.pause_reason = str(state.get("pause_reason", "STATE.md pause flag"))
        result.finished_at = datetime.now(timezone.utc)
        result.summary = f"PAUSED: {result.pause_reason}"
        logger.warning(result.summary)
        write_heartbeat(last_turn=tid, summary=result.summary)
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

    # 2c. Pre-trade analysis — lessons + live EV + portfolio impact → size % or skip
    signals, pretrades = run_pretrade_sizing(
        signals, proposal, turn_id=tid, paper=paper
    )
    skipped = sum(1 for p in pretrades if p.skip)

    # 3. Verification (maker-checker — signal AND allocation/size)
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

    # 4b. Settle expired BTC 5m/15m paper windows (feeds bandit + lessons)
    try:
        from hermes.settlement_fast import settle_expired_paper_positions

        settled = settle_expired_paper_positions(paper=paper)
        if settled:
            logger.info("settled %d expired paper positions", len(settled))
    except Exception as exc:  # noqa: BLE001
        logger.warning("fast settlement skipped: %s", exc)

    # 5. Persistence — lessons from rejections + allocation (settlements arrive async)
    lessons = lessons_engine_tick(
        signals=signals, reports=reports, proposal=proposal
    )
    result.lessons_written = len(lessons)

    # 5b. Autonomy tick — ingest / RASP / EHO / CBPF (non-blocking soft fail)
    try:
        from autonomy.orchestrator import autonomy_tick

        autonomy_tick()
    except Exception as exc:  # noqa: BLE001
        logger.debug("autonomy_tick skipped: %s", exc)

    # Update STATE.md snapshot
    update_state_field("Last Turn", tid)
    update_state_field(
        "Last Turn Summary",
        f"{result.candidates_found} cand / {result.signals_generated} sig / "
        f"skip={skipped} / {result.signals_passed} pass / {result.orders_sent} fills / "
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
    write_heartbeat(
        last_turn=tid,
        summary=result.summary,
        signals_passed=result.signals_passed,
        orders_sent=result.orders_sent,
    )
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
    enforce_paper_only()
    if is_paper_only():
        paper = True
    setup_logging("bot")
    ensure_dirs()
    # Option D — start Binance WS / REST BTC feed before overnight cadence
    try:
        from connectors.cex_realtime import get_feed

        get_feed().start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("CEX realtime feed start failed: %s", exc)
    start_health_server()
    write_heartbeat(summary="overnight_start")
    stop = asyncio.Event()

    async def main_cadence() -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(run_one_turn, paper)
            except Exception as exc:  # noqa: BLE001
                logger.exception("main turn failed: %s", exc)
                write_heartbeat(summary=f"turn_error:{exc}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=main_interval)
            except asyncio.TimeoutError:
                continue

    async def risk_cadence() -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(risk_monitor_tick, paper)
            except Exception as exc:  # noqa: BLE001
                logger.exception("risk tick failed: %s", exc)
            write_heartbeat(summary="risk_ok")
            try:
                await asyncio.wait_for(stop.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                continue

    logger.info(
        "Hermes overnight start paper=%s paper_only=%s loops=%s checkers=%s goals=%s",
        paper,
        is_paper_only(),
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
    enforce_paper_only()
    setup_logging("bot")

    parser = argparse.ArgumentParser(
        description="Financial Freedom Bot — Hermes v2 Paper loop orchestrator"
    )
    parser.add_argument(
        "command",
        choices=["once", "overnight", "goal", "demo", "status"],
        help="once=single turn; overnight=cadence; goal=high-conviction session; demo=full cycle; status=registry",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Blocked when HERMES_PAPER_ONLY=1 (default). Live requires paper-only off + HERMES_LIVE=1.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=300.0,
        help="Main loop interval seconds for overnight mode (default 300)",
    )
    args = parser.parse_args(argv)

    if args.live and is_paper_only():
        logger.error("Refusing --live: HERMES_PAPER_ONLY=1 (Hermes Paper deployment)")
        return 2

    paper = True if is_paper_only() else (not args.live)

    if args.command == "status":
        print("loops:", get_loops())
        print("checkers:", get_checkers())
        print("goals:", get_goals())
        print("paper_only:", is_paper_only())
        return 0
    if args.command == "once":
        start_health_server()
        result = run_one_turn(paper=paper)
        print(result.summary)
        return 0
    if args.command == "goal":
        start_health_server()
        result = high_conviction_session(paper=paper)
        print(result.summary)
        return 0
    if args.command == "demo":
        import os

        os.environ["HERMES_FORCE_SYNTHETIC"] = "1"
        start_health_server()
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
