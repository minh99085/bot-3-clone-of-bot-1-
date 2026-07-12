"""Evidence-based gate auto-tuner (PAPER ONLY).

After each settled directional fill, recompute rolling WR / PnL / fill-rate and nudge
High-WR scalar gates toward the operator target (~1 fill/hour/symbol at healthy WR).

Rules (restrict-only on tighten; loosen only when starved):
  * WR below kill floor + enough samples → tighten (raise min_edge / min_entry / exec EV)
  * WR above target + enough samples → mild tighten (lock in edge)
  * Fill rate too low (starvation) → loosen toward floors
  * Cooldown between adjustments so one trade cannot thrash params
  * Hard clamps keep params inside safe bands (never below paper floors)

Does NOT touch Loop Engineering lanes / maker-checker / coordinator.
Mutates live PulseConfig + hourly/tier/triage mirrors in-process; persists via ledger state.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


@dataclass
class GateAutoTuneBounds:
    min_edge_lo: float = 0.02
    min_edge_hi: float = 0.10
    min_entry_lo: float = 0.45
    min_entry_hi: float = 0.62
    exec_ev_lo: float = 0.0
    exec_ev_hi: float = 0.03
    hourly_sso_lo: float = 600.0
    hourly_sso_hi: float = 1800.0
    sweet_min_lo: float = 0.45
    sweet_min_hi: float = 0.58
    sweet_max_lo: float = 0.65
    sweet_max_hi: float = 0.90


@dataclass
class GateAutoTuneConfig:
    enabled: bool = True
    lookback_n: int = 24
    min_samples: int = 12
    target_wr: float = 0.65
    kill_wr: float = 0.50
    starve_fills_per_hour: float = 0.8   # below this → loosen
    rich_fills_per_hour: float = 3.0     # above this → mild tighten
    step_edge: float = 0.005
    step_entry: float = 0.01
    step_ev: float = 0.002
    step_hourly_s: float = 120.0
    step_sweet: float = 0.01
    cooldown_settlements: int = 6
    bounds: GateAutoTuneBounds = field(default_factory=GateAutoTuneBounds)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


class GateAutoTuner:
    """Rolling settled-outcome tuner for High-WR scalar gates."""

    def __init__(self, cfg: Optional[GateAutoTuneConfig] = None):
        self.cfg = cfg or GateAutoTuneConfig()
        self._recent: Deque[dict] = deque(maxlen=max(8, int(self.cfg.lookback_n)))
        self._since_adjust = 0
        self._adjustments: list = []
        self._last_action: Optional[str] = None
        self._last_ts: Optional[float] = None

    # ---- evidence ----
    def record_settled(
        self,
        *,
        won: bool,
        pnl_usd: float,
        entry_price: Optional[float],
        asset: str = "btc",
        entry_ts: Optional[float] = None,
        now: Optional[float] = None,
    ) -> None:
        if not self.cfg.enabled:
            return
        ts = float(now if now is not None else time.time())
        self._recent.append({
            "won": bool(won),
            "pnl": float(pnl_usd or 0.0),
            "entry_price": (float(entry_price) if entry_price is not None else None),
            "asset": str(asset or "btc").lower(),
            "ts": float(entry_ts) if entry_ts is not None else ts,
            "settled_ts": ts,
        })
        self._since_adjust += 1

    def _rolling(self) -> dict:
        rows = list(self._recent)
        n = len(rows)
        if n == 0:
            return {"n": 0, "wins": 0, "win_rate": None, "pnl_usd": 0.0,
                    "fills_per_hour": 0.0, "by_asset": {}}
        wins = sum(1 for r in rows if r["won"])
        pnl = sum(float(r["pnl"]) for r in rows)
        t0 = min(float(r["settled_ts"]) for r in rows)
        t1 = max(float(r["settled_ts"]) for r in rows)
        hours = max(1.0 / 60.0, (t1 - t0) / 3600.0) if n >= 2 else 1.0
        by_asset: dict = {}
        for r in rows:
            a = r["asset"]
            st = by_asset.setdefault(a, {"n": 0, "wins": 0, "pnl_usd": 0.0})
            st["n"] += 1
            if r["won"]:
                st["wins"] += 1
            st["pnl_usd"] = round(st["pnl_usd"] + float(r["pnl"]), 4)
        for st in by_asset.values():
            st["win_rate"] = round(st["wins"] / st["n"], 4) if st["n"] else None
        return {
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4),
            "pnl_usd": round(pnl, 4),
            "fills_per_hour": round(n / hours, 4),
            "by_asset": by_asset,
            "span_hours": round(hours, 3),
        }

    def _decide(self, roll: dict) -> Optional[str]:
        """Return 'tighten' | 'loosen' | None."""
        n = int(roll.get("n") or 0)
        if n < int(self.cfg.min_samples):
            # starvation path can fire earlier if fill rate is clearly dead
            fph = float(roll.get("fills_per_hour") or 0.0)
            if n >= 4 and fph < float(self.cfg.starve_fills_per_hour) * 0.5:
                return "loosen"
            return None
        wr = roll.get("win_rate")
        fph = float(roll.get("fills_per_hour") or 0.0)
        if wr is None:
            return None
        wr = float(wr)
        if wr < float(self.cfg.kill_wr):
            return "tighten"
        if fph < float(self.cfg.starve_fills_per_hour):
            return "loosen"
        if wr >= float(self.cfg.target_wr) and fph > float(self.cfg.rich_fills_per_hour):
            return "tighten"
        if wr >= float(self.cfg.target_wr) and fph >= float(self.cfg.starve_fills_per_hour):
            return None  # healthy — hold
        if wr < float(self.cfg.target_wr) and fph >= float(self.cfg.rich_fills_per_hour):
            return "tighten"
        return None

    # ---- apply ----
    def maybe_adjust(self, engine) -> Optional[dict]:
        """Mutate engine.cfg (+ hourly/tier/triage mirrors). Returns adjustment dict or None."""
        if not self.cfg.enabled:
            return None
        if self._since_adjust < int(self.cfg.cooldown_settlements):
            return None
        roll = self._rolling()
        action = self._decide(roll)
        if action is None:
            return None

        b = self.cfg.bounds
        cfg = engine.cfg
        before = {
            "min_edge": float(cfg.min_edge),
            "min_entry_price": float(cfg.min_entry_price),
            "exec_min_ev": float(cfg.exec_min_ev_after_slippage),
            "hourly_min_sso": float(getattr(cfg, "hourly_min_seconds_since_open", 300.0)),
            "sweet_min": float(getattr(getattr(engine, "tier_engine", None), "cfg",
                                       None).sweet_min
                               if getattr(engine, "tier_engine", None) is not None else 0.45),
            "sweet_max": float(getattr(getattr(engine, "tier_engine", None), "cfg",
                                       None).sweet_max
                               if getattr(engine, "tier_engine", None) is not None else 0.85),
        }
        sign = 1.0 if action == "tighten" else -1.0

        cfg.min_edge = _clamp(
            before["min_edge"] + sign * float(self.cfg.step_edge),
            b.min_edge_lo, b.min_edge_hi)
        cfg.min_entry_price = _clamp(
            before["min_entry_price"] + sign * float(self.cfg.step_entry),
            b.min_entry_lo, b.min_entry_hi)
        cfg.exec_min_ev_after_slippage = _clamp(
            before["exec_min_ev"] + sign * float(self.cfg.step_ev),
            b.exec_ev_lo, b.exec_ev_hi)
        # hourly: tighten = later entry (higher SSO); loosen = earlier
        new_sso = _clamp(
            before["hourly_min_sso"] + sign * float(self.cfg.step_hourly_s),
            b.hourly_sso_lo, b.hourly_sso_hi)
        cfg.hourly_min_seconds_since_open = new_sso
        if getattr(engine, "hourly_entry_gate", None) is not None:
            engine.hourly_entry_gate.min_seconds_since_open = new_sso

        # sweet band: tighten raises floor / lowers ceiling; loosen opposite
        new_sweet_min = _clamp(
            before["sweet_min"] + sign * float(self.cfg.step_sweet),
            b.sweet_min_lo, b.sweet_min_hi)
        new_sweet_max = _clamp(
            before["sweet_max"] - sign * float(self.cfg.step_sweet),
            b.sweet_max_lo, b.sweet_max_hi)
        if new_sweet_max < new_sweet_min + 0.05:
            new_sweet_max = new_sweet_min + 0.05
        if getattr(engine, "tier_engine", None) is not None:
            te = engine.tier_engine.cfg
            te.sweet_min = new_sweet_min
            te.sweet_max = new_sweet_max
            te.strike_edge_min = max(float(cfg.min_edge), float(te.strike_edge_min) * 0.0
                                     + float(cfg.min_edge))
            te.harvest_edge_min = float(cfg.min_edge)
            te.min_seconds_since_open = float(new_sso)
        # Osmani discovery / evaluator mirrors (captured at init — keep in sync with cfg)
        ol = getattr(engine, "osmani_loop", None)
        if ol is not None:
            disc = getattr(ol, "discovery", None)
            if disc is not None:
                if hasattr(disc, "sweet_min"):
                    disc.sweet_min = new_sweet_min
                if hasattr(disc, "sweet_max"):
                    disc.sweet_max = new_sweet_max
                if hasattr(disc, "min_edge"):
                    disc.min_edge = float(cfg.min_edge)
            triage = (getattr(ol, "_triage_skill", None)
                      or getattr(ol, "triage_skill", None)
                      or (getattr(disc, "_triage", None) if disc is not None else None))
            if triage is not None and getattr(triage, "cfg", None) is not None:
                triage.cfg.sweet_min = new_sweet_min
                triage.cfg.sweet_max = new_sweet_max
            ev = getattr(ol, "_evaluator", None)
            if ev is not None:
                if hasattr(ev, "min_entry_price"):
                    ev.min_entry_price = float(cfg.min_entry_price)
                if hasattr(ev, "min_ev_after_slippage"):
                    ev.min_ev_after_slippage = float(cfg.exec_min_ev_after_slippage)

        after = {
            "min_edge": round(float(cfg.min_edge), 4),
            "min_entry_price": round(float(cfg.min_entry_price), 4),
            "exec_min_ev": round(float(cfg.exec_min_ev_after_slippage), 4),
            "hourly_min_sso": round(float(new_sso), 1),
            "sweet_min": round(float(new_sweet_min), 4),
            "sweet_max": round(float(new_sweet_max), 4),
        }
        adj = {
            "ts": time.time(),
            "action": action,
            "reason": {
                "win_rate": roll.get("win_rate"),
                "fills_per_hour": roll.get("fills_per_hour"),
                "n": roll.get("n"),
                "pnl_usd": roll.get("pnl_usd"),
                "by_asset": roll.get("by_asset"),
                "target_wr": self.cfg.target_wr,
                "kill_wr": self.cfg.kill_wr,
            },
            "before": {k: round(float(v), 4) for k, v in before.items()},
            "after": after,
        }
        self._adjustments.append(adj)
        if len(self._adjustments) > 40:
            self._adjustments = self._adjustments[-40:]
        self._since_adjust = 0
        self._last_action = action
        self._last_ts = adj["ts"]
        return adj

    # ---- persistence / report ----
    def to_state(self) -> dict:
        return {
            "enabled": bool(self.cfg.enabled),
            "recent": list(self._recent),
            "since_adjust": int(self._since_adjust),
            "adjustments": list(self._adjustments[-20:]),
            "last_action": self._last_action,
            "last_ts": self._last_ts,
            "config": {
                "lookback_n": self.cfg.lookback_n,
                "min_samples": self.cfg.min_samples,
                "target_wr": self.cfg.target_wr,
                "kill_wr": self.cfg.kill_wr,
                "starve_fills_per_hour": self.cfg.starve_fills_per_hour,
                "cooldown_settlements": self.cfg.cooldown_settlements,
            },
        }

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self._recent = deque(list(data.get("recent") or []),
                             maxlen=max(8, int(self.cfg.lookback_n)))
        self._since_adjust = int(data.get("since_adjust") or 0)
        self._adjustments = list(data.get("adjustments") or [])[-40:]
        self._last_action = data.get("last_action")
        self._last_ts = data.get("last_ts")

    def report(self) -> dict:
        roll = self._rolling()
        return {
            "enabled": bool(self.cfg.enabled),
            "rolling": roll,
            "since_adjust": int(self._since_adjust),
            "cooldown_settlements": int(self.cfg.cooldown_settlements),
            "last_action": self._last_action,
            "last_ts": self._last_ts,
            "recent_adjustments": list(self._adjustments[-5:]),
            "targets": {
                "target_wr": self.cfg.target_wr,
                "kill_wr": self.cfg.kill_wr,
                "starve_fills_per_hour": self.cfg.starve_fills_per_hour,
                "rich_fills_per_hour": self.cfg.rich_fills_per_hour,
            },
            "note": ("Auto-adjusts min_edge / min_entry / exec_EV / hourly SSO / sweet band "
                     "from settled outcomes. PAPER ONLY. No Loop Engineering changes."),
        }
