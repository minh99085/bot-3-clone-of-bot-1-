"""Addy Osmani 2026 Loop Engineering — decoupled async lanes for Bot-1 (PAPER ONLY).

Three lanes:
  1. Discovery  — sweet-spot trade triage on a timer
  2. Execution  — isolated worktree placement (no shared mutable book state)
  3. Ledger     — single-writer disk persistence + MEMORY.md

Maker-checker: TradeGenerator proposes; TradeEvaluator assumes failure until
independent book API re-fetch confirms the edge.

See docs/osmani-loop-architecture.md
"""

from engine.pulse.loop_architecture.coordinator import OsmaniLoopCoordinator
from engine.pulse.loop_architecture.memory import LoopMemory
from engine.pulse.loop_architecture.circuit_breaker import LoopCircuitBreaker

__all__ = ["OsmaniLoopCoordinator", "LoopMemory", "LoopCircuitBreaker"]
