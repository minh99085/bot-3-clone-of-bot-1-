"""Continuous forever loop — zero babysitting entrypoint.

Runs alongside (or instead of) hermes overnight:
  - Main Hermes turn every HERMES_INTERVAL (default 300s)
  - Autonomy tick every turn (ingest / RASP / EHO / CBPF)
  - Risk guardian already hooked via settlements

Usage:
  export PYTHONPATH=. HERMES_PAPER_ONLY=1 DRY_RUN=true
  python -m autonomy.continuous
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

from hermes.logging_config import enforce_paper_only, is_paper_only, setup_logging

logger = logging.getLogger("autonomy.continuous")


async def run_forever(
    *,
    main_interval: float = 300.0,
    paper: bool = True,
) -> None:
    enforce_paper_only()
    if is_paper_only():
        paper = True
    os.environ.setdefault("DRY_RUN", "true")
    os.environ.setdefault("HERMES_PAPER_ONLY", "1")

    from connectors.cex_realtime import get_feed
    from hermes.health import start_health_server, write_heartbeat
    from hermes.hermes_loop import run_one_turn
    from hermes.risk_monitor import risk_monitor_tick
    from hermes.state_io import ensure_dirs
    from hermes.worktrees import ensure_worktree
    from autonomy.orchestrator import autonomy_tick

    ensure_dirs()
    ensure_worktree("research")
    ensure_worktree("signal")
    ensure_worktree("risk")
    get_feed().start()
    start_health_server()
    write_heartbeat(summary="autonomy_continuous_start")
    logger.info(
        "Autonomy continuous loop start interval=%.0fs paper=%s",
        main_interval,
        paper,
    )

    async def main_cadence() -> None:
        while True:
            try:
                result = await asyncio.to_thread(run_one_turn, paper)
                # Autonomy maintenance after each turn
                summary = await asyncio.to_thread(autonomy_tick)
                logger.info(
                    "autonomy_tick ingest=%s eho=%s rasp=%s",
                    summary.get("ingest_15m"),
                    summary.get("eho", {}).get("reason")
                    or summary.get("eho", {}).get("error")
                    or "skip",
                    (summary.get("rasp") or {}).get("active"),
                )
                write_heartbeat(
                    last_turn=result.turn_id,
                    summary=result.summary,
                    signals_passed=result.signals_passed,
                    orders_sent=result.orders_sent,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("continuous turn failed: %s", exc)
            await asyncio.sleep(main_interval)

    async def risk_cadence() -> None:
        while True:
            try:
                await asyncio.to_thread(risk_monitor_tick, paper)
            except Exception as exc:  # noqa: BLE001
                logger.warning("risk cadence: %s", exc)
            await asyncio.sleep(30.0)

    await asyncio.gather(main_cadence(), risk_cadence())


def main(argv: Optional[list[str]] = None) -> int:
    setup_logging()
    p = argparse.ArgumentParser(description="Hermes autonomy continuous loop")
    p.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("HERMES_INTERVAL", "300")),
        help="Seconds between turns",
    )
    args = p.parse_args(argv)
    try:
        asyncio.run(run_forever(main_interval=args.interval, paper=True))
    except KeyboardInterrupt:
        logger.info("continuous loop stopped")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
