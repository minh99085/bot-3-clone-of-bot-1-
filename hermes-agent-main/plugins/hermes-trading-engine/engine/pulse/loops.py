"""#3 Loop registry + LoopAgent base — formalize the bot's sub-loops (PAPER ONLY).

Loop engineering says every working loop has: an automation (trigger/cadence), a skill, a state, a
verifier, and a verifiable stop-condition. The bot already runs several sub-loops as background
workers; this registry makes them FIRST-CLASS and uniformly observable: each loop declares its
trigger, cadence, skill reference, and a live status provider. ``LoopRegistry.report()`` then gives
one consolidated view of every loop for the dashboard / full report.
"""

from __future__ import annotations

import time
from typing import Callable, Optional


class LoopRegistry:
    """Lightweight registry of the system's sub-loops for uniform observability + a liveness
    WATCHDOG: ``beat(name)`` records a loop's last run; ``report()`` surfaces ``last_beat_age_s`` and
    a ``stalled`` flag so a dead/hung background worker is visible instead of silently reporting a
    stale status."""

    def __init__(self, *, stall_grace_s: float = 60.0, stall_factor: float = 3.0):
        self._loops: dict = {}     # name -> metadata + status provider
        self._beats: dict = {}     # name -> last-run wall-clock
        self.stall_grace_s = float(stall_grace_s)   # min age before a loop can be 'stalled'
        self.stall_factor = float(stall_factor)     # stalled if age > factor * its cadence

    def register(self, name: str, *, role: str, trigger: str, interval_s: Optional[float] = None,
                 skill: Optional[str] = None, verifier: Optional[str] = None,
                 stop_condition: Optional[str] = None,
                 status_fn: Optional[Callable[[], dict]] = None) -> None:
        self._loops[name] = {"role": role, "trigger": trigger, "interval_s": interval_s,
                             "skill": skill, "verifier": verifier, "stop_condition": stop_condition,
                             "status_fn": status_fn}

    def names(self) -> list:
        return list(self._loops.keys())

    def beat(self, name: str, now: Optional[float] = None) -> None:
        """Record that ``name`` just did work (liveness heartbeat for the watchdog)."""
        self._beats[name] = float(now if now is not None else time.time())

    def report(self, now: Optional[float] = None) -> dict:
        now = float(now if now is not None else time.time())
        out = {}
        stalled_any = []
        for name, m in self._loops.items():
            entry = {"role": m["role"], "trigger": m["trigger"], "interval_s": m["interval_s"],
                     "skill": m["skill"], "verifier": m["verifier"],
                     "stop_condition": m["stop_condition"]}
            fn = m.get("status_fn")
            if fn is not None:
                try:
                    st = fn() or {}
                    entry["status"] = {k: st.get(k) for k in
                                       ("enabled", "mode", "calls", "decided", "verified", "errors",
                                        "requested", "tripped") if k in st} or {"reported": True}
                except Exception:  # noqa: BLE001 — status never breaks the report
                    entry["status"] = {"error": True}
            # liveness watchdog: a loop is 'stalled' if it has run before but its last beat is older
            # than max(stall_grace, stall_factor*cadence). Loops never beaten are 'unknown' liveness.
            lb = self._beats.get(name)
            if lb is not None:
                age = max(0.0, now - lb)
                entry["last_beat_age_s"] = round(age, 1)
                iv = m.get("interval_s")
                thresh = max(self.stall_grace_s, self.stall_factor * float(iv)) if iv else None
                if thresh is not None:
                    entry["stalled"] = bool(age > thresh)
                    if entry["stalled"]:
                        stalled_any.append(name)
            out[name] = entry
        return {"loops": out, "count": len(out), "stalled": stalled_any,
                "all_live": (len(stalled_any) == 0),
                "note": ("formalized sub-loops (data/signal/verify/execute/risk/research/news/"
                         "lessons); each has a trigger + skill + verifier + verifiable stop + a "
                         "liveness watchdog (last_beat_age_s/stalled). PAPER ONLY.")}
