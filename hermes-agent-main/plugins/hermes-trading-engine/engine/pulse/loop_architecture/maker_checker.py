"""Maker-checker: TradeGenerator (maker) vs skeptical TradeEvaluator (checker).

The evaluator assumes every trade FAILED until an independent API book re-fetch
confirms the proposed edge still exists at execution time.
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from engine.pulse.execution_gate import evaluate_execution

logger = logging.getLogger("pulse.loop_architecture.maker_checker")


@dataclass
class TradeOpportunity:
    """Discovery lane output — sweet-spot candidate."""
    opportunity_id: str
    event_id: str
    series_slug: str
    side: str
    ask_price: float
    fair_p: float
    edge: float
    size_usd: float
    ttc_s: float
    tick_size: float
    discovered_at: float
    window_snapshot: dict = field(default_factory=dict)


@dataclass
class TradeProposal:
    """Generator output — proposed paper trade (unverified)."""
    proposal_id: str
    opportunity_id: str
    event_id: str
    side: str
    outcome_prob: float
    size_usd: float
    reason: str
    generated_at: float
    worktree_id: str
    context: dict = field(default_factory=dict)
    status: str = "pending_verification"


@dataclass
class VerifiedTrade:
    """Evaluator output — only after independent API check passes."""
    proposal_id: str
    verified: bool
    reason: str
    fill_price: Optional[float] = None
    ev_after_slippage: Optional[float] = None
    verified_at: float = 0.0
    api_check: dict = field(default_factory=dict)


class TradeGenerator:
    """Maker: converts a sweet-spot opportunity into a trade proposal."""

    def propose(self, opp: TradeOpportunity, *, worktree_id: str) -> TradeProposal:
        outcome_prob = float(opp.fair_p if opp.side == "up" else (1.0 - opp.fair_p))
        return TradeProposal(
            proposal_id=str(uuid.uuid4()),
            opportunity_id=opp.opportunity_id,
            event_id=opp.event_id,
            side=opp.side,
            outcome_prob=outcome_prob,
            size_usd=float(opp.size_usd),
            reason=f"sweet_spot_edge={opp.edge:.4f} ask={opp.ask_price:.4f}",
            generated_at=time.time(),
            worktree_id=worktree_id,
            context={
                "series_slug": opp.series_slug,
                "ttc_s": opp.ttc_s,
                "tick_size": opp.tick_size,
                "discovered_edge": opp.edge,
            },
        )


class TradeEvaluator:
    """Skeptical checker: assume failure until independent book API confirms edge."""

    def __init__(
        self,
        *,
        hydrate_fn: Callable[[dict], Any],
        min_ev_after_slippage: float = 0.003,
        max_spread: float = 0.09,
        min_entry_price: float = 0.30,
        max_book_age_s: float = 30.0,
        on_api_call: Optional[Callable[[], None]] = None,
    ):
        self._hydrate = hydrate_fn
        self.min_ev = float(min_ev_after_slippage)
        self.max_spread = float(max_spread)
        self.min_entry_price = float(min_entry_price)
        self.max_book_age_s = float(max_book_age_s)
        self._on_api_call = on_api_call
        self.assumed_failed = 0
        self.verified_ok = 0
        self.verified_reject = 0

    def evaluate(self, proposal: TradeProposal, window_snapshot: dict) -> VerifiedTrade:
        """Re-fetch books via API in isolated worktree; reject unless +EV survives."""
        now = time.time()
        if self._on_api_call:
            self._on_api_call()

        wt = copy.deepcopy(window_snapshot)
        try:
            window = self._hydrate(wt)
        except Exception as exc:
            self.assumed_failed += 1
            self.verified_reject += 1
            return VerifiedTrade(
                proposal_id=proposal.proposal_id,
                verified=False,
                reason=f"api_hydrate_failed:{exc}",
                verified_at=now,
            )

        book = window.up_book if proposal.side == "up" else window.down_book
        ctx = proposal.context or {}
        ex = evaluate_execution(
            side=proposal.side,
            book=book,
            outcome_prob=proposal.outcome_prob,
            size_usd=proposal.size_usd,
            tick_size=float(ctx.get("tick_size") or 0.01),
            ttc_s=float(ctx.get("ttc_s") or 0),
            max_spread=self.max_spread,
            min_ev_after_slippage=self.min_ev,
            min_fill_price=self.min_entry_price,
            taker_fee_rate=float(getattr(window, "taker_fee_rate", 0.0) or 0.0),
            now=now,
            max_book_age_s=self.max_book_age_s,
        )

        if not ex.accepted:
            self.assumed_failed += 1
            self.verified_reject += 1
            return VerifiedTrade(
                proposal_id=proposal.proposal_id,
                verified=False,
                reason=ex.reason,
                verified_at=now,
                api_check={"spread": ex.spread, "ev": ex.ev_after_slippage},
            )

        self.verified_ok += 1
        return VerifiedTrade(
            proposal_id=proposal.proposal_id,
            verified=True,
            reason="independent_api_confirmed",
            fill_price=ex.fill_price,
            ev_after_slippage=ex.ev_after_slippage,
            verified_at=now,
            api_check={
                "spread": ex.spread,
                "vwap": ex.vwap,
                "ev_after_slippage": ex.ev_after_slippage,
            },
        )

    def report(self) -> dict:
        return {
            "assumed_failed_until_verified": True,
            "verified_ok": self.verified_ok,
            "verified_reject": self.verified_reject,
            "assumed_failed": self.assumed_failed,
        }
