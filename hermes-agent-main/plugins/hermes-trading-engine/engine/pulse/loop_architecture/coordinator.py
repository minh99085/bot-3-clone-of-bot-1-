"""Osmani Loop Engineering coordinator — wires Discovery / Execution / Ledger lanes."""

from __future__ import annotations

import logging
import queue
import time
from pathlib import Path
from typing import Any, Callable, Optional

from engine.pulse.loop_architecture.asset_triage import AssetTriageSkill
from engine.pulse.loop_architecture.circuit_breaker import LoopCircuitBreaker
from engine.pulse.loop_architecture.lanes import DiscoveryLane, ExecutionLane, LedgerLane
from engine.pulse.loop_architecture.maker_checker import TradeEvaluator, TradeGenerator
from engine.pulse.loop_architecture.memory import LoopMemory
from engine.pulse.loop_architecture.skill_analysis_boundary import SkillAnalysisBoundary

logger = logging.getLogger("pulse.loop_architecture.coordinator")


class OsmaniLoopCoordinator:
    """Decoupled 3-lane loop architecture (PAPER ONLY)."""

    def __init__(
        self,
        *,
        data_dir: Path,
        windows_fn: Callable[[float], list],
        fair_fn: Callable[[Any, float], Optional[float]],
        hydrate_snapshot_fn: Callable[[dict], Any],
        execute_verified_fn: Callable,
        persist_fn: Callable[[], None],
        capital_fn: Callable[[], dict],
        size_usd: float = 5.0,
        min_edge: float = 0.003,
        discovery_interval_s: float = 60.0,
        exec_min_ev: float = 0.003,
        exec_max_spread: float = 0.09,
        min_entry_price: float = 0.30,
        beat_fn: Optional[Callable[[str, float], None]] = None,
        tv_feature_fn: Optional[Callable[[Any, str, float], Optional[dict]]] = None,
        hourly_entry_fn: Optional[Callable[[Any, float], dict]] = None,
        triage_skill_enabled: bool = True,
        enabled: bool = True,
    ):
        self.enabled = bool(enabled)
        self._data_dir = data_dir
        self.memory = LoopMemory(data_dir)
        self.breaker = LoopCircuitBreaker()
        self._skill_boundary: Optional[SkillAnalysisBoundary] = None
        self._capital_fn = capital_fn
        self._outer_persist_fn = persist_fn
        self._beat = beat_fn
        self._triage_skill = AssetTriageSkill() if triage_skill_enabled else None

        self._discovery_q: queue.Queue = queue.Queue(maxsize=200)
        self._ledger_q: queue.Queue = queue.Queue(maxsize=500)

        self._generator = TradeGenerator()
        self._evaluator = TradeEvaluator(
            hydrate_fn=hydrate_snapshot_fn,
            min_ev_after_slippage=exec_min_ev,
            max_spread=exec_max_spread,
            min_entry_price=min_entry_price,
            on_api_call=self._on_api_call,
        )

        self.discovery = DiscoveryLane(
            out_queue=self._discovery_q,
            windows_fn=windows_fn,
            fair_fn=fair_fn,
            size_usd=size_usd,
            min_edge=min_edge,
            interval_s=discovery_interval_s,
            beat_fn=beat_fn,
            triage_skill=self._triage_skill,
            tv_feature_fn=tv_feature_fn,
            hourly_entry_fn=hourly_entry_fn,
            on_triage_fn=self._on_triage_complete,
        )
        self.execution = ExecutionLane(
            in_queue=self._discovery_q,
            out_queue=self._ledger_q,
            generator=self._generator,
            evaluator=self._evaluator,
            execute_fn=execute_verified_fn,
            beat_fn=beat_fn,
        )
        self.ledger = LedgerLane(
            in_queue=self._ledger_q,
            persist_fn=self._persist_with_breaker,
            memory_save_fn=self._save_memory,
            beat_fn=beat_fn,
        )
        self._started = False

    def _on_api_call(self) -> None:
        if not self.breaker.record_api_call():
            self.breaker.exit_if_tripped()

    def _on_triage_complete(self, verdict) -> None:
        """Skill §4 — write token_id + time boundary to MEMORY.md immediately."""
        if verdict.token_id:
            self.memory.record_triage(
                token_id=str(verdict.token_id),
                time_boundary=str(verdict.time_boundary or ""),
                status=str(verdict.status),
                symbol=str(verdict.symbol or ""),
                timeframe=str(verdict.timeframe or ""),
                side=str(verdict.side or ""),
            )
            self.memory.save()

    def _persist_with_breaker(self) -> None:
        cap = self._capital_fn() or {}
        on_hand = float(cap.get("on_hand_capital_usd") or cap.get("total_on_hand_usd") or 500.0)
        start = float(cap.get("starting_capital_usd") or 500.0)
        if not self.breaker.check_capital(on_hand, start):
            self.breaker.exit_if_tripped()
        self._outer_persist_fn()
        self.breaker.record_lane_success()

    def wake(self) -> dict:
        """Read MEMORY.md + SKILL_ANALYSIS.md on process start (disk-bound, no guessing)."""
        boundary = SkillAnalysisBoundary.load(self._data_dir)
        self._skill_boundary = boundary
        if self._triage_skill is not None:
            self._triage_skill.cfg = boundary.to_triage_config()
        self.discovery.sweet_min = boundary.sweet_min
        self.discovery.sweet_max = boundary.sweet_max

        mem = self.memory.load()
        logger.info(
            "Osmani loop wake #%s — MEMORY.md loaded (%d recent decisions); "
            "SKILL_ANALYSIS loaded=%s hash=%s",
            mem.get("wake_count"),
            len(mem.get("recent_decisions") or []),
            boundary.loaded,
            boundary.content_hash or "none",
        )
        return {"memory": mem, "skill_analysis": boundary.report()}

    def _save_memory(self, patch: dict) -> None:
        cap = self._capital_fn() or {}
        self.memory.update_capital(cap)
        self.memory.update_lane_status({
            "discovery": self.discovery.report(),
            "execution": self.execution.report(),
            "ledger": self.ledger.report(),
            "evaluator": self._evaluator.report(),
            "circuit_breaker": self.breaker.report(),
        })
        for ex in (patch.get("recent_execution") or [])[-5:]:
            p = ex.get("proposal") or {}
            self.memory.record_decision(
                event_id=str(p.get("event_id", "")),
                side=str(p.get("side", "")),
                status="verified" if ex.get("verified") else f"rejected:{ex.get('verify_reason')}",
            )
        self.memory.save()

    def start(self) -> None:
        if not self.enabled or self._started:
            return
        self.wake()
        self.discovery.start()
        self.execution.start()
        self.ledger.start()
        self._started = True
        logger.info("Osmani 3-lane loop started (discovery / execution / ledger)")

    def stop(self) -> None:
        self.discovery.stop()
        self.execution.stop()
        self.ledger.stop()
        self._started = False

    def tick_boundary(self) -> None:
        """Called from main tick — capital check + sync ledger flush."""
        if not self.enabled:
            return
        cap = self._capital_fn() or {}
        on_hand = float(cap.get("on_hand_capital_usd") or 500.0)
        start = float(cap.get("starting_capital_usd") or 500.0)
        if not self.breaker.check_capital(on_hand, start):
            self.breaker.exit_if_tripped()
        self.ledger.enqueue_memory_update({"capital": cap})
        self.ledger.flush_sync()

    def report(self) -> dict:
        return {
            "enabled": self.enabled,
            "architecture": "osmani_loop_engineering_2026",
            "lanes": {
                "discovery": self.discovery.report(),
                "execution": self.execution.report(),
                "ledger": self.ledger.report(),
            },
            "maker_checker": {
                "generator": "TradeGenerator",
                "evaluator": self._evaluator.report(),
            },
            "asset_triage_skill": (
                self._triage_skill.report() if self._triage_skill is not None else {"enabled": False}
            ),
            "skill_analysis_boundary": (
                self._skill_boundary.report() if self._skill_boundary is not None else None
            ),
            "memory": {
                "path": str(self.memory.path),
                "wake_count": self.memory.data.get("wake_count"),
            },
            "circuit_breaker": self.breaker.report(),
        }
