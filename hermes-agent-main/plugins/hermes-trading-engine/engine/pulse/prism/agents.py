"""PRISM Phase 6 — Sniper / Harvester agents + capital allocation + sizing (PAPER ONLY).

Two directional agents act on the PRISM rank ``R = I * max(0,E) * C``:

    SNIPER    R >= r_min_sniper, I >= i_floor, C >= c_floor   -> big, selective (10-20% slice)
    HARVESTER r_min_harvester <= R < r_harvester_max          -> small, frequent (15% slice)
    NONE      otherwise

Capital is sliced: arb reserve 40% (untouched), sniper 35%, harvester 15%, buffer 10%. Position
size follows::

    raw = bankroll * agent_slice * tanh(R / 0.08) * C * thompson_mult
    caps: half-Kelly, 25% of book depth, SNIPER <= $200 / HARVESTER <= $25
    daily_loss_halt: agent slice down >= 12% today -> size 0 until reset

Restrict-only + PAPER ONLY: agents can size DOWN or block a paper candidate; they can never force a
fill or bypass the execution gate. Observe-only until the operator promotes the agent gate.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AgentKind(str, Enum):
    SNIPER = "sniper"
    HARVESTER = "harvester"
    NONE = "none"


@dataclass
class AgentConfig:
    r_min_sniper: float = 0.12
    i_floor_sniper: float = 0.70
    c_floor_sniper: float = 0.75
    r_min_harvester: float = 0.03
    r_max_harvester: float = 0.06
    arb_reserve_frac: float = 0.40
    sniper_frac: float = 0.35
    harvester_frac: float = 0.15
    buffer_frac: float = 0.10
    sniper_max_usd: float = 200.0
    harvester_max_usd: float = 25.0
    depth_cap_frac: float = 0.25
    daily_loss_halt_pct: float = 0.12
    r_size_scale: float = 0.08          # tanh(R / r_size_scale)

    @classmethod
    def from_env(cls) -> "AgentConfig":
        def _f(key: str, default: float) -> float:
            try:
                return float(os.getenv(key, str(default)))
            except (TypeError, ValueError):
                return default
        return cls(
            r_min_sniper=_f("PULSE_PRISM_SNIPER_R_MIN", 0.12),
            i_floor_sniper=_f("PULSE_PRISM_I_FLOOR_SNIPER", 0.70),
            c_floor_sniper=_f("PULSE_PRISM_SNIPER_C_FLOOR", 0.75),
            r_min_harvester=_f("PULSE_PRISM_HARVESTER_R_MIN", 0.03),
            r_max_harvester=_f("PULSE_PRISM_HARVESTER_R_MAX", 0.06),
            sniper_max_usd=_f("PULSE_PRISM_SNIPER_MAX_USD", 200.0),
            harvester_max_usd=_f("PULSE_PRISM_HARVESTER_MAX_USD", 25.0),
            daily_loss_halt_pct=_f("PULSE_PRISM_DAILY_LOSS_HALT_PCT", 0.12),
        )


def classify_agent(R: float, I: float, C: float, cfg: AgentConfig) -> AgentKind:
    """Assign a directional agent from the PRISM rank + information + confidence."""
    if (R >= cfg.r_min_sniper and I >= cfg.i_floor_sniper and C >= cfg.c_floor_sniper):
        return AgentKind.SNIPER
    if cfg.r_min_harvester <= R < cfg.r_max_harvester:
        return AgentKind.HARVESTER
    return AgentKind.NONE


@dataclass
class SizingResult:
    agent: AgentKind
    size_usd: float
    raw_usd: float
    caps_applied: list
    halted: bool

    def to_dict(self) -> dict:
        return {"agent": self.agent.value, "size_usd": round(self.size_usd, 4),
                "raw_usd": round(self.raw_usd, 4), "caps_applied": list(self.caps_applied),
                "halted": self.halted}


class CapitalAllocator:
    """Slice-based sizing + per-agent daily-loss halt. PAPER ONLY."""

    def __init__(self, bankroll_usd: float, cfg: Optional[AgentConfig] = None):
        self.bankroll_usd = float(bankroll_usd)
        self.cfg = cfg or AgentConfig()
        self._day_key: Optional[int] = None
        self._daily_pnl: dict = {AgentKind.SNIPER.value: 0.0, AgentKind.HARVESTER.value: 0.0}

    def _slice_frac(self, agent: AgentKind) -> float:
        if agent == AgentKind.SNIPER:
            return self.cfg.sniper_frac
        if agent == AgentKind.HARVESTER:
            return self.cfg.harvester_frac
        return 0.0

    def _agent_cap(self, agent: AgentKind) -> float:
        if agent == AgentKind.SNIPER:
            return self.cfg.sniper_max_usd
        if agent == AgentKind.HARVESTER:
            return self.cfg.harvester_max_usd
        return 0.0

    def record_pnl(self, agent: AgentKind, pnl_usd: float, *, now: Optional[float] = None) -> None:
        """Track realized PnL per agent per UTC day (for the daily-loss halt)."""
        import time as _t
        day = int((now if now is not None else _t.time()) // 86400)
        if day != self._day_key:
            self._day_key = day
            self._daily_pnl = {AgentKind.SNIPER.value: 0.0, AgentKind.HARVESTER.value: 0.0}
        if agent.value in self._daily_pnl:
            self._daily_pnl[agent.value] += float(pnl_usd or 0.0)

    def _halted(self, agent: AgentKind) -> bool:
        slice_usd = self._slice_frac(agent) * self.bankroll_usd
        if slice_usd <= 0:
            return False
        loss = -min(0.0, self._daily_pnl.get(agent.value, 0.0))
        return loss >= self.cfg.daily_loss_halt_pct * slice_usd

    def size_usd(self, agent: AgentKind, R: float, C: float, ask: Optional[float],
                 depth_usd: Optional[float], thompson_mult: float = 1.0,
                 open_corr: float = 0.0, p_win: Optional[float] = None) -> SizingResult:
        """PRISM position size for a paper candidate, with all caps + daily-loss halt applied."""
        caps: list = []
        if agent == AgentKind.NONE:
            return SizingResult(agent, 0.0, 0.0, ["agent_none"], False)
        if self._halted(agent):
            return SizingResult(agent, 0.0, 0.0, ["daily_loss_halt"], True)

        slice_frac = self._slice_frac(agent)
        raw = (self.bankroll_usd * slice_frac * math.tanh(max(0.0, R) / self.cfg.r_size_scale)
               * max(0.0, min(1.0, C)) * max(0.0, min(1.0, thompson_mult)))
        size = raw

        # half-Kelly cap (0/1 payout bought at ``ask``): kelly f* = (p - ask) / (1 - ask)
        if p_win is not None and ask is not None and 0.0 < float(ask) < 1.0:
            kelly_f = max(0.0, (float(p_win) - float(ask)) / (1.0 - float(ask)))
            kelly_cap = 0.5 * kelly_f * self.bankroll_usd
            if size > kelly_cap:
                size = kelly_cap
                caps.append("half_kelly")

        # book-depth cap (never consume more than depth_cap_frac of resting depth)
        if depth_usd is not None and depth_usd > 0:
            depth_cap = self.cfg.depth_cap_frac * float(depth_usd)
            if size > depth_cap:
                size = depth_cap
                caps.append("depth_25pct")

        # correlated-exposure haircut
        if open_corr > 0:
            size *= max(0.0, 1.0 - float(open_corr))
            caps.append("open_corr")

        # agent hard cap
        cap = self._agent_cap(agent)
        if size > cap:
            size = cap
            caps.append("agent_cap")

        return SizingResult(agent, max(0.0, size), raw, caps, False)


# ---- Adversarial checks (restrict-only): shrink confidence/size, or flag a stale-book edge ---- #

def adjust_confidence_spread_widening(C: float, spread_now: Optional[float],
                                      spread_60s_ago: Optional[float]) -> float:
    """If the spread widened >=2x in the last minute, cut confidence to 0.7x (adverse liquidity)."""
    if spread_now is None or spread_60s_ago is None or spread_60s_ago <= 0:
        return C
    if float(spread_now) >= 2.0 * float(spread_60s_ago):
        return C * 0.7
    return C


def adjust_size_depth_drop(size_usd: float, depth_now: Optional[float],
                           depth_60s_ago: Optional[float]) -> float:
    """If resting depth dropped >=50% in the last minute, halve the size."""
    if depth_now is None or depth_60s_ago is None or depth_60s_ago <= 0:
        return size_usd
    if float(depth_now) <= 0.5 * float(depth_60s_ago):
        return size_usd * 0.5
    return size_usd


def boost_rank_stale_book(R: float, cex_move_bps: Optional[float], ask_move: Optional[float],
                          *, cex_thr_bps: float = 5.0, ask_thr: float = 0.01,
                          boost: float = 1.15) -> float:
    """CEX moved materially but the poly ask barely moved -> stale book -> boost R (lead-lag edge)."""
    if cex_move_bps is None or ask_move is None:
        return R
    if abs(float(cex_move_bps)) >= cex_thr_bps and abs(float(ask_move)) <= ask_thr:
        return R * boost
    return R
