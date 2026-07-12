"""PRISM Phase 3 — optimal stopping engine: ENTER / WAIT / SKIP (PAPER ONLY).

Each tick the bot must decide whether to ENTER a directional candidate now, WAIT for more
information, or SKIP the window entirely. It does **not** enter on the first edge pass — it weighs
the value of acting now against the value of waiting for information completeness ``I(t)`` to rise::

    R        = I * max(0, E) * C
    V_enter  = max(0, E) * C * payoff_shape(ask)          # payoff_shape favors the 0.47–0.55 sweet spot
    V_wait   = discount * expected_future_R * (1 - I/I_target) - opportunity_cost_lambda

    ENTER  if R >= r_min AND V_enter > V_wait AND the FSM state allows an entry
    SKIP   if the window is expired (>50m), E < 0, or R has declined 3 consecutive ticks
    WAIT   otherwise (esp. while I < I_target and the window is still open)

Restrict-only and PAPER ONLY: this engine can WAIT/SKIP a candidate or let it pass to the existing
execution gate, but it can never force a fill. In Phase 3 the ensemble edge ``E`` is a placeholder
(0.0) — so the safe default is WAIT until Phase 4 supplies a real ``E``.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from engine.pulse.prism.information import (
    FSMState,
    max_tier_for_state,
    state_from_seconds_since_open,
)

# Seconds-since-open past which no new entry is taken (matches the FSM EXPIRED boundary).
_EXPIRED_S = 50 * 60.0


class StoppingDecision(str, Enum):
    ENTER = "enter"
    WAIT = "wait"
    SKIP = "skip"


@dataclass
class PRISMConfig:
    """Optimal-stopping tunables (env-loadable). PAPER ONLY."""

    r_min_sniper: float = 0.12
    r_min_harvester: float = 0.03
    i_floor_sniper: float = 0.70
    i_target: float = 0.78
    opportunity_cost_lambda: float = 0.002
    discount: float = 0.98
    edge_velocity_enter_boost: float = 0.15   # lower effective r_min when edge velocity v_E > 0

    @classmethod
    def from_env(cls) -> "PRISMConfig":
        def _f(key: str, default: float) -> float:
            try:
                return float(os.getenv(key, str(default)))
            except (TypeError, ValueError):
                return default
        return cls(
            r_min_sniper=_f("PULSE_PRISM_SNIPER_R_MIN", 0.12),
            r_min_harvester=_f("PULSE_PRISM_HARVESTER_R_MIN", 0.03),
            i_floor_sniper=_f("PULSE_PRISM_I_FLOOR_SNIPER", 0.70),
            i_target=_f("PULSE_PRISM_I_TARGET", 0.78),
            opportunity_cost_lambda=_f("PULSE_PRISM_OPPORTUNITY_COST_LAMBDA", 0.002),
            discount=_f("PULSE_PRISM_DISCOUNT", 0.98),
            edge_velocity_enter_boost=_f("PULSE_PRISM_EDGE_VELOCITY_ENTER_BOOST", 0.15),
        )


@dataclass
class StoppingState:
    """Everything the stopping rule needs for one window at one tick."""

    seconds_since_open: float
    ttc_s: float
    I: float
    E: float
    C: float
    R: float
    v_E: float
    state_fsm: FSMState
    belief_posterior_p: Optional[float] = None
    ask_price: Optional[float] = None
    side: Optional[str] = None
    r_declining: bool = False


@dataclass
class StoppingResult:
    decision: StoppingDecision
    reason: str
    R: float
    v_E: float
    eff_r_min: float
    V_enter: float
    V_wait: float
    tier: str
    state_fsm: str

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "R": round(self.R, 6),
            "v_E": round(self.v_E, 6),
            "eff_r_min": round(self.eff_r_min, 6),
            "V_enter": round(self.V_enter, 6),
            "V_wait": round(self.V_wait, 6),
            "tier": self.tier,
            "fsm_state": self.state_fsm,
        }


def payoff_shape(ask: Optional[float]) -> float:
    """Payoff-quality multiplier in [0,1] that favors the 0.47–0.55 sweet spot.

    Full credit inside the sweet band; a linear taper (floored at 0.2) outside it — cheap tails and
    expensive favorites are penalized relative to near-even books.
    """
    if ask is None:
        return 0.5
    try:
        a = float(ask)
    except (TypeError, ValueError):
        return 0.5
    if 0.47 <= a <= 0.55:
        return 1.0
    dist = (0.47 - a) if a < 0.47 else (a - 0.55)
    return max(0.2, 1.0 - 3.0 * dist)


def optimal_stopping_decide(state: StoppingState, cfg: PRISMConfig) -> StoppingResult:
    """Core ENTER/WAIT/SKIP rule. Pure function of ``state`` + ``cfg``."""
    tier = max_tier_for_state(state.state_fsm)   # none | harvester | sniper

    # Effective entry threshold from the tier, lowered when edge velocity is positive.
    if tier == "sniper":
        base_r_min = cfg.r_min_sniper
    elif tier == "harvester":
        base_r_min = cfg.r_min_harvester
    else:
        base_r_min = float("inf")               # no entry tier available in this FSM state
    eff_r_min = base_r_min
    if state.v_E > 0 and base_r_min != float("inf"):
        eff_r_min = base_r_min * (1.0 - cfg.edge_velocity_enter_boost)

    V_enter = max(0.0, state.E) * state.C * payoff_shape(state.ask_price)

    # Project what R could become if information rises to the target (R scales ~ linearly with I).
    if state.I > 1e-6:
        projected_R = min(1.0, state.R * (cfg.i_target / state.I))
    else:
        projected_R = cfg.r_min_sniper           # optimistic prior when nothing has arrived yet
    incompleteness = max(0.0, 1.0 - (state.I / cfg.i_target if cfg.i_target > 0 else 1.0))
    V_wait = cfg.discount * projected_R * incompleteness - cfg.opportunity_cost_lambda

    def _result(dec: StoppingDecision, reason: str) -> StoppingResult:
        return StoppingResult(decision=dec, reason=reason, R=state.R, v_E=state.v_E,
                              eff_r_min=(0.0 if eff_r_min == float("inf") else eff_r_min),
                              V_enter=V_enter, V_wait=V_wait, tier=tier,
                              state_fsm=state.state_fsm.value)

    # ---- hard SKIP conditions (no new entry) ----
    if state.seconds_since_open >= _EXPIRED_S or state.state_fsm == FSMState.EXPIRED:
        return _result(StoppingDecision.SKIP, "expired")
    if state.E < 0.0:
        return _result(StoppingDecision.SKIP, "negative_edge")
    if state.r_declining:
        return _result(StoppingDecision.SKIP, "rank_declining_3_ticks")

    # ---- ENTER ----
    if tier != "none" and state.R >= eff_r_min and V_enter > V_wait:
        return _result(StoppingDecision.ENTER, "rank_clears_threshold")

    # ---- WAIT (default; still gathering information) ----
    if state.I < cfg.i_target and state.seconds_since_open < _EXPIRED_S:
        return _result(StoppingDecision.WAIT, "info_incomplete")
    return _result(StoppingDecision.WAIT, "rank_below_threshold")


class StoppingEngine:
    """Per-window stopping state: edge/rank ring buffers, decision, and observe-only counters."""

    def __init__(self, cfg: Optional[PRISMConfig] = None, *, history: int = 4):
        self.cfg = cfg or PRISMConfig()
        self._history = int(history)
        self._E_hist: dict[str, deque] = {}
        self._R_hist: dict[str, deque] = {}
        self.counts: dict[str, int] = {"enter": 0, "wait": 0, "skip": 0}

    def reset_window(self, window_key: str) -> None:
        self._E_hist.pop(window_key, None)
        self._R_hist.pop(window_key, None)

    def _push(self, window_key: str, E: float, R: float) -> None:
        eb = self._E_hist.setdefault(window_key, deque(maxlen=self._history))
        rb = self._R_hist.setdefault(window_key, deque(maxlen=self._history))
        eb.append(float(E))
        rb.append(float(R))

    @staticmethod
    def _velocity(buf: deque) -> float:
        if len(buf) < 2:
            return 0.0
        return float(buf[-1]) - float(buf[-2])

    @staticmethod
    def _declining_3(buf: deque) -> bool:
        # 3 consecutive strict declines requires 4 successive samples.
        if len(buf) < 4:
            return False
        b = list(buf)[-4:]
        return b[0] > b[1] > b[2] > b[3]

    def evaluate(self, window_key: str, *, sso: float, ttc_s: float, I: float, E: float, C: float,
                 belief_posterior_p: Optional[float] = None, ask_price: Optional[float] = None,
                 side: Optional[str] = None) -> StoppingResult:
        """Push the tick's (E, R) onto the window buffers and return the stopping decision."""
        R = float(I) * max(0.0, float(E)) * float(C)
        self._push(window_key, E, R)
        v_E = self._velocity(self._E_hist[window_key])
        declining = self._declining_3(self._R_hist[window_key])
        state = StoppingState(
            seconds_since_open=float(sso), ttc_s=float(ttc_s), I=float(I), E=float(E), C=float(C),
            R=R, v_E=v_E, state_fsm=state_from_seconds_since_open(sso),
            belief_posterior_p=belief_posterior_p, ask_price=ask_price, side=side,
            r_declining=declining)
        result = optimal_stopping_decide(state, self.cfg)
        self.counts[result.decision.value] = self.counts.get(result.decision.value, 0) + 1
        return result

    def to_report(self) -> dict:
        total = sum(self.counts.values())
        return {
            "enabled": True,
            "counts": dict(self.counts),
            "total_decisions": total,
            "wait_rate": round(self.counts.get("wait", 0) / total, 4) if total else None,
            "cfg": {
                "r_min_sniper": self.cfg.r_min_sniper,
                "r_min_harvester": self.cfg.r_min_harvester,
                "i_target": self.cfg.i_target,
                "opportunity_cost_lambda": self.cfg.opportunity_cost_lambda,
                "discount": self.cfg.discount,
                "edge_velocity_enter_boost": self.cfg.edge_velocity_enter_boost,
            },
        }
