"""Three decoupled async lanes: Discovery, Execution (worktree), Ledger (single writer)."""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from engine.pulse.loop_architecture.asset_triage import AssetTriageSkill, TriageVerdict
from engine.pulse.loop_architecture.maker_checker import (
    TradeEvaluator,
    TradeGenerator,
    TradeOpportunity,
    TradeProposal,
    VerifiedTrade,
)

logger = logging.getLogger("pulse.loop_architecture.lanes")

SWEET_SPOT_MIN = 0.48
SWEET_SPOT_MAX = 0.72


@dataclass
class LedgerWriteJob:
    """Lane 3 job — serialized disk persistence."""
    job_id: str
    kind: str
    payload: dict
    enqueued_at: float = field(default_factory=time.time)


class DiscoveryLane:
    """Lane 1: timer-driven sweet-spot (0.47–0.55) trade triage."""

    def __init__(
        self,
        *,
        out_queue: queue.Queue,
        windows_fn: Callable[[float], list],
        fair_fn: Callable[[Any, float], Optional[float]],
        size_usd: float = 5.0,
        min_edge: float = 0.003,
        interval_s: float = 15.0,
        sweet_min: float = SWEET_SPOT_MIN,
        sweet_max: float = SWEET_SPOT_MAX,
        triage_skill: Optional[AssetTriageSkill] = None,
        tv_feature_fn: Optional[Callable[[Any, str, float], Optional[dict]]] = None,
        hourly_entry_fn: Optional[Callable[[Any, float], dict]] = None,
        on_triage_fn: Optional[Callable[[TriageVerdict], None]] = None,
        beat_fn: Optional[Callable[[str, float], None]] = None,
    ):
        self._out = out_queue
        self._windows_fn = windows_fn
        self._fair_fn = fair_fn
        self.size_usd = float(size_usd)
        self.min_edge = float(min_edge)
        self.interval_s = float(interval_s)
        self.sweet_min = float(sweet_min)
        self.sweet_max = float(sweet_max)
        self._triage = triage_skill
        self._tv_feature_fn = tv_feature_fn
        self._hourly_entry_fn = hourly_entry_fn
        self._on_triage = on_triage_fn
        self._beat = beat_fn
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.scans = 0
        self.emitted = 0
        self.triage_rejected = 0
        self.hourly_entry_blocked = 0

    def _emit_opportunity(self, w, side: str, ask_f: float, fair: float, now: float,
                          verdict: Optional[TriageVerdict] = None) -> None:
        edge = float((fair if side == "up" else (1.0 - fair)) - ask_f)
        if edge < self.min_edge:
            return
        from engine.pulse.execution_gate import down_ask_fair_gap_blocks
        import os
        max_gap = float(os.getenv("PULSE_DOWN_MAX_ASK_FAIR_GAP", "0.12") or 0.12)
        if down_ask_fair_gap_blocks(side=side, ask=ask_f, fair_p_up=fair, max_gap=max_gap):
            self.triage_rejected += 1
            return
        mode = (verdict.status if verdict else "TIMER_SWEEP")
        opp = TradeOpportunity(
            opportunity_id=str(uuid.uuid4()),
            event_id=w.event_id,
            series_slug=getattr(w, "series_slug", ""),
            side=side,
            ask_price=ask_f,
            fair_p=float(fair),
            edge=edge,
            size_usd=self.size_usd,
            ttc_s=float(w.seconds_to_close(now)),
            tick_size=float(getattr(w, "tick_size", 0.01) or 0.01),
            discovered_at=now,
            window_snapshot={
                "event_id": w.event_id,
                "series_slug": getattr(w, "series_slug", ""),
                "tick_size": getattr(w, "tick_size", 0.01),
                "up_token_id": getattr(w, "up_token_id", None),
                "down_token_id": getattr(w, "down_token_id", None),
                "market_id": getattr(w, "market_id", None),
                "close_ts": getattr(w, "close_ts", None),
                "open_ts": getattr(w, "open_ts", None),
                "triage_status": mode,
                "tv_timeframe": (verdict.timeframe if verdict else None),
                "tv_symbol": (verdict.symbol if verdict else None),
            },
        )
        try:
            self._out.put_nowait(opp)
            self.emitted += 1
        except queue.Full:
            logger.warning("discovery queue full — dropping opportunity %s", w.event_id)

    def _scan_once(self, now: float) -> None:
        self.scans += 1
        for w in self._windows_fn(now):
            if self._hourly_entry_fn is not None:
                he = self._hourly_entry_fn(w, now)
                if he.get("decision") == "reject":
                    self.hourly_entry_blocked += 1
                    continue
            fair = self._fair_fn(w, now)
            if fair is None:
                continue
            series = getattr(w, "series_slug", "")
            for side, book, p_win in (
                ("up", w.up_book, fair),
                ("down", w.down_book, 1.0 - fair),
            ):
                ask = getattr(book, "best_ask", None) if book else None
                if ask is None:
                    continue
                ask_f = float(ask)
                in_sweet = self.sweet_min <= ask_f <= self.sweet_max
                in_tail = ask_f < 0.10
                if not in_sweet and not in_tail:
                    continue

                if self._triage is not None and self._tv_feature_fn is not None:
                    from engine.pulse.tradingview import tv_symbol_for_window
                    sym = tv_symbol_for_window(w) or ""
                    tv_feat = self._tv_feature_fn(w, sym, now)
                    verdict = self._triage.evaluate(
                        window=w, side=side, ask_price=ask_f, now=now,
                        tv_feature=tv_feat, symbol=sym,
                    )
                    if self._on_triage:
                        self._on_triage(verdict)
                    if not verdict.proceed:
                        self.triage_rejected += 1
                        continue
                    self._emit_opportunity(w, side, ask_f, fair, now, verdict)
                    continue

                if not in_sweet:
                    continue
                self._emit_opportunity(w, side, ask_f, fair, now)

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            try:
                self._scan_once(now)
                if self._beat:
                    self._beat("osmani_discovery", now)
            except Exception:
                logger.exception("discovery lane error")
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="osmani-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def report(self) -> dict:
        rep = {"scans": self.scans, "emitted": self.emitted, "interval_s": self.interval_s,
               "triage_rejected": self.triage_rejected,
               "hourly_entry_blocked": self.hourly_entry_blocked}
        if self._triage is not None:
            rep["asset_triage_skill"] = self._triage.report()
        return rep


class ExecutionLane:
    """Lane 2: isolated worktree execution — generator then skeptical evaluator."""

    def __init__(
        self,
        *,
        in_queue: queue.Queue,
        out_queue: queue.Queue,
        generator: TradeGenerator,
        evaluator: TradeEvaluator,
        execute_fn: Callable[[TradeProposal, VerifiedTrade, dict], bool],
        beat_fn: Optional[Callable[[str, float], None]] = None,
    ):
        self._in = in_queue
        self._out = out_queue
        self._generator = generator
        self._evaluator = evaluator
        self._execute_fn = execute_fn
        self._beat = beat_fn
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.processed = 0
        self.executed = 0
        self.rejected = 0
        self._worktrees: dict[str, dict] = {}

    def _worktree_create(self, snapshot: dict) -> str:
        wid = str(uuid.uuid4())
        self._worktrees[wid] = dict(snapshot)
        if len(self._worktrees) > 50:
            for k in list(self._worktrees.keys())[:-50]:
                self._worktrees.pop(k, None)
        return wid

    def _worktree_get(self, wid: str) -> dict:
        return dict(self._worktrees.get(wid) or {})

    def _worktree_destroy(self, wid: str) -> None:
        self._worktrees.pop(wid, None)

    def _process(self, opp: TradeOpportunity) -> None:
        wid = self._worktree_create(opp.window_snapshot)
        proposal = self._generator.propose(opp, worktree_id=wid)
        snapshot = self._worktree_get(wid)
        verified = self._evaluator.evaluate(proposal, snapshot)
        self.processed += 1
        if verified.verified:
            ok = self._execute_fn(proposal, verified, snapshot)
            if ok:
                self.executed += 1
            else:
                self.rejected += 1
        else:
            self.rejected += 1
        self._out.put(LedgerWriteJob(
            job_id=str(uuid.uuid4()),
            kind="execution_result",
            payload={
                "proposal": {
                    "proposal_id": proposal.proposal_id,
                    "event_id": proposal.event_id,
                    "side": proposal.side,
                    "reason": proposal.reason,
                },
                "verified": verified.verified,
                "verify_reason": verified.reason,
                "fill_price": verified.fill_price,
            },
        ))
        self._worktree_destroy(wid)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                opp = self._in.get(timeout=0.5)
            except queue.Empty:
                continue
            now = time.time()
            try:
                self._process(opp)
                if self._beat:
                    self._beat("osmani_execution", now)
            except Exception:
                logger.exception("execution lane error")
            finally:
                self._in.task_done()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="osmani-execution", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def report(self) -> dict:
        return {
            "processed": self.processed,
            "executed": self.executed,
            "rejected": self.rejected,
            "open_worktrees": len(self._worktrees),
        }


class LedgerLane:
    """Lane 3: single-writer persistence queue (MEMORY.md + engine persist)."""

    def __init__(
        self,
        *,
        in_queue: queue.Queue,
        persist_fn: Callable[[], None],
        memory_save_fn: Callable[[dict], None],
        beat_fn: Optional[Callable[[str, float], None]] = None,
    ):
        self._in = in_queue
        self._persist_fn = persist_fn
        self._memory_save_fn = memory_save_fn
        self._beat = beat_fn
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.writes = 0
        self._pending_memory: dict = {}

    def enqueue_memory_update(self, patch: dict) -> None:
        self._pending_memory.update(patch or {})

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._in.get(timeout=0.5)
            except queue.Empty:
                continue
            now = time.time()
            try:
                if job.kind == "execution_result":
                    self._pending_memory.setdefault("recent_execution", []).append(job.payload)
                self._memory_save_fn(self._pending_memory)
                self._persist_fn()
                self.writes += 1
                if self._beat:
                    self._beat("osmani_ledger", now)
            except Exception:
                logger.exception("ledger lane persist error")
            finally:
                self._in.task_done()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="osmani-ledger", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def flush_sync(self) -> None:
        """Synchronous persist (tick boundary) — drains memory patch then writes."""
        try:
            self._memory_save_fn(self._pending_memory)
            self._persist_fn()
            self.writes += 1
        except Exception:
            logger.exception("ledger lane sync flush error")

    def report(self) -> dict:
        return {"writes": self.writes}
