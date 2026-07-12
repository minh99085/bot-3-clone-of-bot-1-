"""TradingView Context Gate (restrict-only, PAPER ONLY).

A hard, prior-based companion to the Learned Selectivity Gate. Where the selectivity gate needs
enough *settled samples* before it will reject a bucket, this gate enforces the analyst's already-
established findings IMMEDIATELY (before samples accumulate): the bot's own signal-learning report
showed entry contexts that consistently lose on the BTC 5-min market — TradingView ``volume_state``
spikes (WR ~0.13, large negative PnL), the ``noise`` hurst regime, and entries too far from
resolution (``ttc`` >= ~240s, WR ~0.2).

It sits between the directional decision and the execution gate and can ONLY make the bot more
selective — it can never create, force, resize, or fast-track a trade, and the strict execution
gate remains the sole trade authority. A small, hard-capped exploration rate lets a fraction of
otherwise-blocked candidates through (tagged ``explore``) so the bot keeps confirming that those
contexts remain bad rather than going blind to them.

Pure + fully unit-testable: the engine passes in the already-computed context values.
"""

from __future__ import annotations

import random
from typing import Optional


def _norm_set(values) -> "tuple[str, ...]":
    return tuple(sorted({str(v).strip().lower() for v in (values or []) if str(v).strip()}))


class TradingViewContextGate:
    """Restrict-only context gate. ``evaluate`` returns a decision dict:
    ``{"decision": "pass"|"block"|"explore", "reasons": [...]}``.

    Blocks when the entry context matches a proven-losing rule:
      * ``volume_state`` (from the TradingView alert) in ``blocked_volume_states`` (default spike)
      * ``hurst_regime`` (bot research feature) in ``blocked_hurst_regimes`` (default noise)
      * ``ttc_s`` (seconds to window close) >= ``max_ttc_s`` (default 240) — entered too early
    """

    def __init__(self, *, enabled: bool = False,
                 blocked_volume_states=("spike",),
                 blocked_hurst_regimes=("noise",),
                 max_ttc_s: Optional[float] = 240.0,
                 block_liquidation_spike: bool = True,
                 block_event_blackout: bool = True,
                 block_grok_event_risk_high: bool = True,
                 exploration_rate: float = 0.05, seed: Optional[int] = None):
        self.enabled = bool(enabled)
        self.blocked_volume_states = _norm_set(blocked_volume_states)
        self.blocked_hurst_regimes = _norm_set(blocked_hurst_regimes)
        self.max_ttc_s = (float(max_ttc_s) if max_ttc_s is not None else None)
        self.block_liquidation_spike = bool(block_liquidation_spike)
        self.block_event_blackout = bool(block_event_blackout)
        self.block_grok_event_risk_high = bool(block_grok_event_risk_high)
        # hard cap exploration at 5% so the gate can never quietly become permissive
        self.exploration_rate = max(0.0, min(0.05, float(exploration_rate)))
        self.passed = 0
        self.blocked = 0
        self.explored = 0
        self.block_reasons: dict = {}      # reason -> count (actually blocked)
        self.explore_reasons: dict = {}    # reason -> count (matched a rule but explored through)
        self._rng = random.Random(seed)

    # -- pure rule evaluation (no counters / RNG) --------------------------- #
    def violations(self, *, volume_state=None, hurst_regime=None,
                   ttc_s: Optional[float] = None,
                   liquidation_spike=None, event_blackout=None,
                   grok_event_risk=None) -> "list[str]":
        reasons = []
        vs = str(volume_state or "").strip().lower()
        if vs and vs in self.blocked_volume_states:
            reasons.append("tv_context_volume_" + vs)
        hr = str(hurst_regime or "").strip().lower()
        if hr and hr in self.blocked_hurst_regimes:
            reasons.append("tv_context_hurst_" + hr)
        if (self.max_ttc_s is not None and ttc_s is not None
                and float(ttc_s) >= self.max_ttc_s):
            reasons.append("tv_context_ttc_too_far")
        if self.block_liquidation_spike and liquidation_spike is True:
            reasons.append("tv_context_liquidation_spike")
        if self.block_event_blackout and event_blackout is True:
            reasons.append("tv_context_event_blackout")
        if self.block_grok_event_risk_high:
            er = str(grok_event_risk or "").strip().lower()
            if er == "high":
                reasons.append("tv_context_grok_event_risk_high")
        return reasons

    def evaluate(self, *, volume_state=None, hurst_regime=None,
                 ttc_s: Optional[float] = None,
                 liquidation_spike=None, event_blackout=None,
                 grok_event_risk=None) -> dict:
        if not self.enabled:
            return {"decision": "pass", "reasons": [], "active": False}
        v = self.violations(volume_state=volume_state, hurst_regime=hurst_regime, ttc_s=ttc_s,
                            liquidation_spike=liquidation_spike, event_blackout=event_blackout,
                            grok_event_risk=grok_event_risk)
        if not v:
            self.passed += 1
            return {"decision": "pass", "reasons": [], "active": True}
        if self.exploration_rate > 0 and self._rng.random() < self.exploration_rate:
            self.explored += 1
            for r in v:
                self.explore_reasons[r] = self.explore_reasons.get(r, 0) + 1
            return {"decision": "explore", "reasons": v, "active": True, "exploration": True}
        self.blocked += 1
        self.block_reasons[v[0]] = self.block_reasons.get(v[0], 0) + 1
        return {"decision": "block", "reasons": v, "active": True}

    def report(self) -> dict:
        return {
            "enabled": self.enabled,
            "mode": "restrict_only_context_prior",
            "affects_trading": self.enabled,
            "can_force_trade": False,
            "execution_gate_still_authoritative": True,
            "blocked_volume_states": list(self.blocked_volume_states),
            "blocked_hurst_regimes": list(self.blocked_hurst_regimes),
            "max_ttc_s": self.max_ttc_s,
            "block_liquidation_spike": self.block_liquidation_spike,
            "block_event_blackout": self.block_event_blackout,
            "block_grok_event_risk_high": self.block_grok_event_risk_high,
            "exploration_rate": self.exploration_rate,
            "passed": self.passed, "blocked": self.blocked, "explored": self.explored,
            "block_reasons": dict(self.block_reasons),
            "explore_reasons": dict(self.explore_reasons),
            "note": ("hard prior gate: blocks proven-losing entry contexts (volume spikes, noise "
                     "regime, far-from-resolution) before enough samples exist for the learned "
                     "selectivity gate. Can only PREVENT trades — never forces, sizes, or bypasses "
                     "the execution gate, which remains the sole trade authority."),
        }

    def to_state(self) -> dict:
        return {"passed": self.passed, "blocked": self.blocked, "explored": self.explored,
                "block_reasons": dict(self.block_reasons),
                "explore_reasons": dict(self.explore_reasons)}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.passed = int(data.get("passed", 0) or 0)
        self.blocked = int(data.get("blocked", 0) or 0)
        self.explored = int(data.get("explored", 0) or 0)
        self.block_reasons = {k: int(v or 0) for k, v in (data.get("block_reasons") or {}).items()}
        self.explore_reasons = {k: int(v or 0)
                                for k, v in (data.get("explore_reasons") or {}).items()}
