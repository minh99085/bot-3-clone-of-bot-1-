"""Risk monitor — parallel worktree, never blocks the execution path.

Runs on a fast cadence (@loop 30s/1m). Hard kill switch on drawdown,
daily loss, consecutive losses, and model-drift proxies. Writes pause flags
into STATE.md so discovery/verifier self-halt on the next tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from hermes.decorators import loop
from hermes.models import RiskSnapshot
from hermes.state_io import (
    append_jsonl,
    ensure_dirs,
    ledger_path,
    parse_state_fields,
    read_jsonl,
    read_state_md,
    update_state_field,
    knowledge_path,
)
from hermes.worktrees import ensure_worktree

logger = logging.getLogger(__name__)

MAX_DRAWDOWN_PCT = 0.08
MAX_DAILY_LOSS_PCT = 0.03
MAX_CONSECUTIVE_LOSSES = 4
MIN_ROLLING_WR_20 = 0.55  # soft pause if rolling WR collapses
MIN_ROLLING_PF_20 = 1.2
MAX_OPEN_EXPOSURE_PCT = 0.20


def _recent_settlements(paper: bool = True, n: int = 50) -> list[dict]:
    rows = read_jsonl(ledger_path(paper=paper))
    settles = [r for r in rows if r.get("event") == "settlement" or r.get("won") is not None]
    return settles[-n:]


def _rolling_stats(settles: list[dict]) -> tuple[float, float, int]:
    if not settles:
        return 1.0, 2.0, 0  # optimistic cold start — verifier still gates
    wins = [s for s in settles if s.get("won") or s.get("pnl_usd", 0) > 0]
    losses = [s for s in settles if not (s.get("won") or s.get("pnl_usd", 0) > 0)]
    wr = len(wins) / len(settles)
    gross_win = sum(float(s.get("pnl_usd", 0)) for s in wins) or 0.0
    gross_loss = abs(sum(float(s.get("pnl_usd", 0)) for s in losses)) or 1e-9
    pf = gross_win / gross_loss if gross_loss else 99.0
    # consecutive losses from end
    consec = 0
    for s in reversed(settles):
        if s.get("won") or float(s.get("pnl_usd", 0)) > 0:
            break
        consec += 1
    return wr, pf, consec


def compute_risk_snapshot(state: Optional[dict] = None, paper: bool = True) -> RiskSnapshot:
    state = state if state is not None else parse_state_fields(read_state_md())
    capital = float(state.get("capital_usd", state.get("capital", 10_000)) or 10_000)
    open_exp = float(state.get("open_exposure_usd", 0) or 0)
    daily_pnl = float(state.get("daily_pnl_usd", 0) or 0)
    dd = float(state.get("max_drawdown_pct", state.get("drawdown_pct", 0)) or 0)

    settles = _recent_settlements(paper=paper, n=20)
    wr, pf, consec = _rolling_stats(settles)

    trip = False
    reasons: list[str] = []

    if dd >= MAX_DRAWDOWN_PCT:
        trip = True
        reasons.append(f"max_drawdown={dd:.2%}>={MAX_DRAWDOWN_PCT:.0%}")
    if daily_pnl <= -capital * MAX_DAILY_LOSS_PCT:
        trip = True
        reasons.append(f"daily_loss={daily_pnl:.2f}")
    if consec >= MAX_CONSECUTIVE_LOSSES:
        trip = True
        reasons.append(f"consecutive_losses={consec}")
    if open_exp > capital * MAX_OPEN_EXPOSURE_PCT:
        trip = True
        reasons.append(f"open_exposure={open_exp:.2f}")

    pause = trip
    # Soft performance gates (Hermes weakness fix)
    if len(settles) >= 10 and wr < MIN_ROLLING_WR_20:
        pause = True
        reasons.append(f"rolling_wr_20={wr:.2%}<{MIN_ROLLING_WR_20:.0%}")
    if len(settles) >= 10 and pf < MIN_ROLLING_PF_20:
        pause = True
        reasons.append(f"rolling_pf_20={pf:.2f}<{MIN_ROLLING_PF_20}")

    return RiskSnapshot(
        capital_usd=capital,
        open_exposure_usd=open_exp,
        daily_pnl_usd=daily_pnl,
        rolling_wr_20=round(wr, 4),
        rolling_pf_20=round(pf, 4),
        max_drawdown_pct=dd,
        consecutive_losses=consec,
        circuit_breaker_tripped=trip,
        trip_reason="; ".join(reasons),
        pause_loop=pause,
    )


def apply_risk_to_state(snap: RiskSnapshot) -> None:
    """Persist kill-switch into STATE.md so other loops halt."""
    update_state_field("Rolling WR (20)", f"{snap.rolling_wr_20:.2%}")
    update_state_field("Rolling PF (20)", f"{snap.rolling_pf_20:.2f}")
    update_state_field("Consecutive Losses", str(snap.consecutive_losses))
    update_state_field("Circuit Breaker", "TRIPPED" if snap.circuit_breaker_tripped else "clear")
    update_state_field("Pause Loop", "true" if snap.pause_loop else "false")
    if snap.pause_loop:
        update_state_field("Pause Reason", snap.trip_reason or "performance gate")
        logger.error("RISK HALT: %s", snap.trip_reason)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    append_jsonl(
        ledger_path(paper=True).parent / "risk_snapshots.jsonl",
        snap,
    )
    # Touch a marker file in risk worktree
    marker = knowledge_path("STATE.md")
    _ = marker, stamp


@loop(interval="30s", name="risk_monitor")
def risk_monitor_tick(paper: bool = True) -> RiskSnapshot:
    ensure_dirs()
    ensure_worktree("risk")  # parallel lane — does not share signal worktree
    state = parse_state_fields(read_state_md())
    snap = compute_risk_snapshot(state=state, paper=paper)
    apply_risk_to_state(snap)
    logger.info(
        "risk: dd=%.2f%% wr20=%.2f pf20=%.2f consec=%d pause=%s",
        snap.max_drawdown_pct * 100,
        snap.rolling_wr_20,
        snap.rolling_pf_20,
        snap.consecutive_losses,
        snap.pause_loop,
    )
    return snap
