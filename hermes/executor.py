"""Executor — only executes verifier-PASSED signals.

Uses worktree isolation for the live signal lane. Paper mode is the default;
live requires explicit HERMES_LIVE=1 and STATE.md live_enabled=true.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from hermes.decorators import loop
from hermes.models import (
    Fill,
    OrderIntent,
    Position,
    Signal,
    VerificationReport,
    VerifierDecision,
)
from hermes.state_io import (
    append_jsonl,
    ensure_dirs,
    ledger_path,
    parse_state_fields,
    read_state_md,
    write_handoff,
)
from hermes.worktrees import ensure_worktree

logger = logging.getLogger(__name__)


def _paper_mode(state: dict) -> bool:
    # Hermes Paper: hard lock — never live
    if os.environ.get("HERMES_PAPER_ONLY", "1").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("HERMES_LIVE", "0") == "1" and state.get("live_enabled"):
        return False
    return True


def build_intent(
    signal: Signal,
    report: VerificationReport,
    *,
    paper: bool,
) -> OrderIntent:
    limit = signal.entry_vwap_target if signal.entry_vwap_target is not None else signal.market_price
    return OrderIntent(
        signal_id=signal.signal_id,
        market_id=signal.market_id,
        direction=signal.direction,
        size_usd=report.sized_usd or signal.size_usd_suggested,
        limit_price=float(limit),
        entry_mode=signal.entry_mode,
        paper=paper,
    )


def execute_intent(intent: OrderIntent, signal: Optional[Signal] = None) -> Fill:
    """Route to broker. Paper fills use CLOB book + Chainlink context when available."""
    try:
        from connectors.broker import BrokerClient

        broker = BrokerClient(paper=intent.paper)
        token_id = signal.clob_token_id if signal else None
        asset = (signal.meta or {}).get("asset") if signal else None
        return broker.execute(intent, token_id=token_id, asset=asset)
    except Exception as exc:  # noqa: BLE001
        logger.warning("broker connector fallback fill (%s)", exc)
        slip = 0.002
        fill_price = intent.limit_price + (
            slip if intent.direction.value in ("YES", "UP") else -slip
        )
        fill_price = min(0.99, max(0.01, fill_price))
        fees = intent.size_usd * 0.01
        return Fill(
            intent_id=intent.intent_id,
            signal_id=intent.signal_id,
            market_id=intent.market_id,
            direction=intent.direction,
            size_usd=intent.size_usd,
            fill_price=fill_price,
            fees_usd=fees,
            slippage_bps=slip * 10_000,
            paper=intent.paper,
        )


def open_position(fill: Fill) -> Position:
    return Position(
        signal_id=fill.signal_id,
        market_id=fill.market_id,
        direction=fill.direction,
        size_usd=fill.size_usd,
        entry_price=fill.fill_price,
        paper=fill.paper,
    )


@loop(interval="5m", name="executor")
def executor_tick(
    signals: Optional[list[Signal]] = None,
    reports: Optional[list[VerificationReport]] = None,
    turn_id: Optional[str] = None,
) -> list[Fill]:
    """Execute only PASS verifications. Isolates work in signal worktree metadata."""
    ensure_dirs()
    ensure_worktree("signal")  # isolation boundary for live lane artifacts

    if not signals or not reports:
        return []

    state = parse_state_fields(read_state_md())
    paper = _paper_mode(state)
    by_id = {s.signal_id: s for s in signals}
    fills: list[Fill] = []
    positions: list[Position] = []

    for report in reports:
        if report.decision != VerifierDecision.PASS:
            continue
        signal = by_id.get(report.signal_id)
        if signal is None:
            logger.error("PASS report %s has no matching signal", report.signal_id)
            continue
        meta = signal.meta or {}
        if meta.get("enhanced_misprice") and not meta.get("enhanced_passes"):
            logger.warning(
                "executor skip %s: enhanced_passes=False (%s)",
                signal.signal_id,
                (meta.get("enhanced_reasons") or [])[:2],
            )
            continue
        intent = build_intent(signal, report, paper=paper)
        fill = execute_intent(intent, signal=signal)
        pos = open_position(fill)
        fills.append(fill)
        positions.append(pos)
        append_jsonl(ledger_path(paper=paper), {
            "event": "fill",
            **fill.model_dump(mode="json"),
            "slug": signal.slug,
            "meta": {
                **(signal.meta or {}),
                "slug": signal.slug,
                "substrategy_id": signal.substrategy_id,
                "market_series": signal.market_series,
                "timeframe": signal.timeframe,
            },
        })
        append_jsonl(ledger_path(paper=paper), {
            "event": "position_open",
            **pos.model_dump(mode="json"),
            "slug": signal.slug,
            "meta": {
                **(signal.meta or {}),
                "slug": signal.slug,
                "substrategy_id": signal.substrategy_id,
                "market_series": signal.market_series,
                "timeframe": signal.timeframe,
                "entry_source": (signal.meta or {}).get("entry_source"),
                "bandit_arm": (signal.meta or {}).get("bandit_arm"),
                "bandit_context": (signal.meta or {}).get("bandit_context"),
                "cex_mid": (signal.meta or {}).get("cex_mid"),
                "cex_asset": (signal.meta or {}).get("cex_asset")
                or (signal.meta or {}).get("asset")
                or "BTC",
            },
        })
        logger.info(
            "EXECUTE %s %s $%.2f @ %.3f paper=%s",
            fill.direction.value,
            fill.market_id,
            fill.size_usd,
            fill.fill_price,
            fill.paper,
        )

    tid = turn_id or "adhoc"
    write_handoff("fills", fills, tid)
    write_handoff("positions", positions, tid)
    return fills
