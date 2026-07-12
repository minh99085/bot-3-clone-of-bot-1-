"""Short-term Markov regime machine for the BTC 5-min pulse (OBSERVE-ONLY).

Classifies each candidate into a discrete regime state, learns transition counts/probabilities
from paper history, and (when enough per-state samples exist) emits conditional outputs:
``p_resolve_up``, ``p_resolve_down``, ``p_edge_survives_execution``, ``p_no_trade_best``.

OBSERVE-ONLY: states + probabilities are logged + reported; they never trade, size, or veto.
Sparse-sample safe: outputs are None with an explicit reason until ``min_samples`` is reached.
"""

from __future__ import annotations

from typing import Optional

STATES = ("trend_up", "trend_down", "mean_reverting_up", "mean_reverting_down", "chop_noise",
          "stale_polymarket_up", "stale_polymarket_down", "liquidity_danger",
          "resolution_danger")


def classify_state(*, hurst_regime: Optional[str], signal_direction: Optional[str],
                   stale_factor: Optional[float], settlement_boundary_risk: Optional[float],
                   spread: Optional[float], ask_depth_usd: Optional[float],
                   min_depth_usd: float = 50.0, max_spread: float = 0.06,
                   stale_threshold: float = 0.5) -> str:
    """Deterministically map the current context to one regime state (priority: danger first)."""
    if settlement_boundary_risk and settlement_boundary_risk >= 0.5:
        return "resolution_danger"
    if (ask_depth_usd is not None and ask_depth_usd < min_depth_usd) or \
            (spread is not None and spread > max_spread):
        return "liquidity_danger"
    up = signal_direction == "up"
    down = signal_direction == "down"
    if stale_factor is not None and stale_factor >= stale_threshold:
        return "stale_polymarket_up" if up else ("stale_polymarket_down" if down else "chop_noise")
    if hurst_regime == "trending":
        return "trend_up" if up else ("trend_down" if down else "chop_noise")
    if hurst_regime == "mean_reverting":
        return "mean_reverting_up" if up else ("mean_reverting_down" if down else "chop_noise")
    return "chop_noise"


class MarkovRegime:
    def __init__(self, *, min_samples: int = 20):
        self.min_samples = int(min_samples)
        self._last_state: Optional[str] = None
        self.transitions: dict = {}                 # from -> {to: count}
        self.state_counts: dict = {s: 0 for s in STATES}
        # per-state accumulators: n candidates, edge_survived (gate-accepted), no_trade_best
        # (no trade taken); resolved_* track Up/Down for windows that actually settled.
        self.outcomes: dict = {s: {"n": 0, "edge_survived": 0, "no_trade_best": 0,
                                   "resolved_n": 0, "resolved_up": 0} for s in STATES}

    def observe(self, state: str) -> None:
        if state not in self.state_counts:
            return
        self.state_counts[state] += 1
        if self._last_state is not None:
            row = self.transitions.setdefault(self._last_state, {})
            row[state] = row.get(state, 0) + 1
        self._last_state = state

    def record_terminal(self, *, state: Optional[str], accepted: bool) -> None:
        """One row per candidate at its terminal: did the edge survive execution (accepted)?"""
        if state not in self.outcomes:
            return
        o = self.outcomes[state]
        o["n"] += 1
        o["edge_survived"] += int(bool(accepted))
        o["no_trade_best"] += int(not accepted)

    def record_resolution(self, *, state: Optional[str], outcome_up: Optional[bool]) -> None:
        """A settled window's Up/Down resolution, attributed to its entry-time state."""
        if state not in self.outcomes or outcome_up is None:
            return
        o = self.outcomes[state]
        o["resolved_n"] += 1
        o["resolved_up"] += int(bool(outcome_up))

    def transition_probs(self) -> dict:
        out = {}
        for frm, row in self.transitions.items():
            total = sum(row.values())
            if total > 0:
                out[frm] = {to: round(c / total, 4) for to, c in row.items()}
        return out

    def state_outputs(self, state: str) -> dict:
        """Conditional outputs for a state, or None+reason when samples are insufficient."""
        o = self.outcomes.get(state, {"n": 0, "resolved_n": 0})
        n = o["n"]
        rn = o["resolved_n"]
        if n < self.min_samples:
            return {"state": state, "samples": n, "resolved_samples": rn, "observe_only": True,
                    "reason": "insufficient_samples", "p_resolve_up": None, "p_resolve_down": None,
                    "p_edge_survives_execution": None, "p_no_trade_best": None}
        p_up = (o["resolved_up"] / rn) if rn >= self.min_samples else None
        return {"state": state, "samples": n, "resolved_samples": rn, "observe_only": True,
                "reason": "ok",
                "p_resolve_up": (round(p_up, 4) if p_up is not None else None),
                "p_resolve_down": (round(1.0 - p_up, 4) if p_up is not None else None),
                "p_edge_survives_execution": round(o["edge_survived"] / n, 4),
                "p_no_trade_best": round(o["no_trade_best"] / n, 4)}

    def report(self) -> dict:
        return {"enabled": True, "observe_only": True, "affects_trading": False,
                "min_samples": self.min_samples,
                "state_coverage": dict(self.state_counts),
                "transition_probs": self.transition_probs(),
                "state_outputs": {s: self.state_outputs(s) for s in STATES
                                  if self.outcomes[s]["n"] > 0}}
