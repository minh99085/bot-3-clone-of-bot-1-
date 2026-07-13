"""Directional cell learning table — Phase 1 observe + Phase 2 posterior (PAPER ONLY).

Each behavioral cell is keyed by::

    (asset, horizon, side, minute_band, regime, tv_pattern, ask_band)

Phase 1 logs every tier-engine evaluation and grades settled trades. Wilson bounds drive
FOLLOW / FADE / OBSERVE verdicts per cell.

Phase 2 (directional lane only, ``PULSE_CELL_LEARNING_PHASE2_ENABLED``) nudges the tier posterior
and size from mature cell verdicts — not a hard gate; execution_gate stays authoritative.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine.pulse.prism.belief import logit, sigmoid
from engine.pulse.signal_edge import FADE, FOLLOW, OBSERVE, classify_signal, wilson_bounds

# Phase 2 posterior nudges (directional lane only; env-overridable via engine cfg).
PHASE2_FOLLOW_LOGIT = 0.20
PHASE2_FADE_LOGIT = -0.30
PHASE2_OBSERVE_LOGIT = 0.0
PHASE2_FOLLOW_SIZE_MULT = 1.20
PHASE2_FADE_SIZE_MULT = 0.45
PHASE2_OBSERVE_SIZE_MULT = 0.85

# TV ladder TFs used for cell tv_pattern encoding (includes 45m intrahour).
_CELL_TV_TFS = ("5", "15", "30", "45")

logger = logging.getLogger("pulse.directional_cell_learning")

_STORE_FILENAME = "directional_cell_learning.json"

# Phase 1 minute bands (1h window study).
_MINUTE_BANDS = ((0, 5), (5, 12), (12, 30), (30, 45), (45, 55))

# Default Polymarket study surface: hourly + 15m BTC/ETH directional lanes.
PHASE1_SERIES_SLUGS = (
    "btc-up-or-down-hourly",
    "btc-up-or-down-15m",
    "btc-up-or-down-4h",
    "eth-up-or-down-hourly",
    "eth-up-or-down-15m",
    "eth-up-or-down-4h",
)

_STRONG_THRESH = 0.80
_INFO_THRESH = 0.45


def minute_band_from_seconds(sso: Optional[float]) -> str:
    """Map seconds-since-open to a Phase-1 minute band."""
    if sso is None:
        return "other"
    try:
        m = float(sso) / 60.0
    except (TypeError, ValueError):
        return "other"
    for lo, hi in _MINUTE_BANDS:
        if lo <= m < hi:
            return "%d-%dm" % (lo, hi)
    return "other"


def ask_band_from_price(ask: Optional[float], *, sweet_min: float = 0.47,
                        sweet_max: float = 0.55) -> str:
    """Bucket entry ask into sweet / mid / tail."""
    if ask is None:
        return "unknown"
    try:
        p = float(ask)
    except (TypeError, ValueError):
        return "unknown"
    if sweet_min <= p <= sweet_max:
        return "sweet"
    if 0.30 <= p < sweet_min or sweet_max < p <= 0.70:
        return "mid"
    return "tail"


def _dir_sign(d) -> int:
    s = str(d or "").strip().upper()
    if s in ("UP", "LONG", "BUY", "BULL"):
        return 1
    if s in ("DOWN", "SHORT", "SELL", "BEAR"):
        return -1
    return 0


def tv_pattern_from_ladder(tv_by_tf: Optional[dict], side: Optional[str], *,
                           information_I: Optional[float] = None,
                           strong_thresh: float = _STRONG_THRESH,
                           info_thresh: float = _INFO_THRESH) -> str:
    """Encode the TV ladder into S+/S-/W+/W-/C/H/∅ for the proposed side.

    - ∅  no fresh MTF reads
    - H  high information score (PRISM I) regardless of direction
    - C  MTF conflict (5/15/30m disagree)
    - S+/S-  strong (strength >= thresh) aligned / opposed to side
    - W+/W-  weak aligned / opposed
    """
    if information_I is not None and float(information_I) >= info_thresh:
        return "H"
    ladder = tv_by_tf or {}
    signs = []
    strengths = []
    for tf in _CELL_TV_TFS:
        snap = ladder.get(tf) or {}
        sgn = _dir_sign(snap.get("direction"))
        if sgn == 0:
            continue
        signs.append(sgn)
        strengths.append(float(snap.get("strength") or 0.5))
    if not signs:
        return "empty"
    up_n = sum(1 for s in signs if s > 0)
    dn_n = sum(1 for s in signs if s < 0)
    if up_n > 0 and dn_n > 0:
        return "C"
    dominant = 1 if up_n >= dn_n else -1
    avg_str = sum(strengths) / len(strengths)
    side_sign = 1 if str(side or "").lower() == "up" else (-1 if str(side or "").lower() == "down" else 0)
    if side_sign == 0:
        return "C"
    aligned = dominant == side_sign
    strong = avg_str >= strong_thresh
    if strong and aligned:
        return "S+"
    if strong and not aligned:
        return "S-"
    if aligned:
        return "W+"
    return "W-"


def asset_from_series(series_slug: Optional[str], series_label: Optional[str] = None) -> str:
    """Extract asset head from a Polymarket series slug/label."""
    blob = " ".join((str(series_slug or ""), str(series_label or ""))).lower()
    if "ethereum" in blob or blob.startswith("eth") or "eth_" in blob:
        return "eth"
    if "bitcoin" in blob or blob.startswith("btc") or "btc_" in blob:
        return "btc"
    slug = str(series_slug or series_label or "").strip().lower()
    if slug.startswith("eth"):
        return "eth"
    head = str(series_label or "").split("_")[0].lower()
    if head in ("btc", "eth"):
        return head
    return "btc"


@dataclass(frozen=True)
class CellKey:
    asset: str
    minute_band: str
    regime: str
    tv_pattern: str
    ask_band: str
    horizon: str = "unknown"
    side: str = "unknown"

    def as_str(self) -> str:
        return "|".join((self.asset, self.horizon, self.side, self.minute_band,
                         self.regime, self.tv_pattern, self.ask_band))

    @classmethod
    def from_str(cls, s: str) -> "CellKey":
        parts = s.split("|")
        if len(parts) >= 7:
            return cls(parts[0], parts[3], parts[4], parts[5], parts[6], parts[1], parts[2])
        legacy = (parts + ["", "", "", "", ""])[:5]
        return cls(*legacy)


@dataclass
class CellStats:
    evals: int = 0
    trades: int = 0
    wins: int = 0
    pnl_usd: float = 0.0

    def to_dict(self) -> dict:
        return {"evals": self.evals, "trades": self.trades, "wins": self.wins,
                "pnl_usd": round(self.pnl_usd, 4)}

    @classmethod
    def from_dict(cls, d: dict) -> "CellStats":
        return cls(evals=int(d.get("evals", 0)), trades=int(d.get("trades", 0)),
                   wins=int(d.get("wins", 0)), pnl_usd=float(d.get("pnl_usd", 0.0)))


@dataclass
class CellEvalSnapshot:
    """Pending eval tags keyed by window for settlement grading."""
    cell_key: str
    tier: str
    side: Optional[str]
    edge: float
    p_up: float
    series_slug: str


class DirectionalCellLearningStore:
    """Disk-bound observe-only cell table. PAPER ONLY."""

    def __init__(self, data_dir: Optional[Path] = None, *, min_samples: int = 30):
        self.data_dir = Path(data_dir) if data_dir else None
        self.min_samples = int(min_samples)
        self.cells: dict[str, CellStats] = {}
        self._pending: dict[str, CellEvalSnapshot] = {}
        if self.data_dir is not None:
            self.load()

    @property
    def path(self) -> Optional[Path]:
        return (self.data_dir / _STORE_FILENAME) if self.data_dir is not None else None

    def key_from_context(self, *, series_slug: str, series_label: Optional[str],
                         sso: Optional[float], regime: str, tv_by_tf: Optional[dict],
                         side: Optional[str], ask: Optional[float],
                         information_I: Optional[float] = None) -> CellKey:
        return CellKey(
            asset=asset_from_series(series_slug, series_label),
            minute_band=minute_band_from_seconds(sso),
            regime=str(regime or "unknown"),
            tv_pattern=tv_pattern_from_ladder(tv_by_tf, side, information_I=information_I),
            ask_band=ask_band_from_price(ask),
            horizon=("1h" if "hourly" in str(series_slug).lower()
                     or str(series_label or "").lower().endswith("_1h") else "15m"),
            side=str(side or "unknown").lower(),
        )

    def get(self, key: CellKey) -> CellStats:
        k = key.as_str()
        if k not in self.cells:
            self.cells[k] = CellStats()
        return self.cells[k]

    def log_eval(self, window_key: str, key: CellKey, *, tier: str, side: Optional[str],
                 edge: float, p_up: float, series_slug: str, traded: bool = False,
                 save: bool = True) -> None:
        """Record a tier-engine evaluation (every tick, traded or not)."""
        stats = self.get(key)
        stats.evals += 1
        self._pending[str(window_key)] = CellEvalSnapshot(
            cell_key=key.as_str(), tier=str(tier), side=side,
            edge=float(edge or 0.0), p_up=float(p_up or 0.5),
            series_slug=str(series_slug or ""))
        if save:
            self.save()

    def record_settled(self, window_key: str, *, won: bool, pnl_usd: float,
                       research: Optional[dict] = None, save: bool = True) -> None:
        """Grade a settled directional trade against its entry cell."""
        # The persisted position is the immutable entry snapshot.  `_pending` continues to hold
        # counterfactual tick evaluations and must never override what actually produced the fill.
        pending = self._pending.pop(str(window_key), None)
        snap = None
        if research:
            ck = research.get("cell_learning_key")
            if ck:
                snap = CellEvalSnapshot(
                    cell_key=str(ck), tier=str(research.get("cell_learning_tier") or ""),
                    side=research.get("cell_learning_side"),
                    edge=float(research.get("cell_learning_edge") or 0.0),
                    p_up=float(research.get("cell_learning_p_up") or 0.5),
                    series_slug=str(research.get("series_slug") or ""))
        if snap is None:
            snap = pending
        if snap is None:
            return
        stats = self.cells.setdefault(snap.cell_key, CellStats())
        stats.trades += 1
        if won:
            stats.wins += 1
        stats.pnl_usd += float(pnl_usd or 0.0)
        if save:
            self.save()

    def cell_verdict(self, key: CellKey) -> dict:
        stats = self.get(key)
        n = stats.trades
        if n <= 0:
            return {"n": 0, "verdict": "observe", "reason": "no_settled_trades"}
        lo, hi = wilson_bounds(stats.wins, n)
        wr = stats.wins / n
        cl = classify_signal(n, wr, min_samples=self.min_samples)
        return {**cl, "trades": n, "evals": stats.evals, "pnl_usd": round(stats.pnl_usd, 4),
                "cell": key.as_str()}

    def phase2_adjustment(self, key: CellKey) -> dict:
        """Posterior nudge from a cell's Wilson verdict (Phase 2, directional lane only)."""
        v = self.cell_verdict(key)
        verdict = str(v.get("verdict") or OBSERVE)
        if verdict == FOLLOW:
            logit_shift, size_mult = PHASE2_FOLLOW_LOGIT, PHASE2_FOLLOW_SIZE_MULT
        elif verdict == FADE:
            logit_shift, size_mult = PHASE2_FADE_LOGIT, PHASE2_FADE_SIZE_MULT
        else:
            logit_shift, size_mult = PHASE2_OBSERVE_LOGIT, PHASE2_OBSERVE_SIZE_MULT
        # Constants are expressed relative to the chosen side.  Convert them to UP log-odds;
        # otherwise FOLLOW on a DOWN cell incorrectly strengthens UP (and FADE strengthens DOWN).
        if key.side == "down":
            logit_shift = -logit_shift
        return {
            "enabled": True,
            "verdict": verdict,
            "logit_shift": logit_shift,
            "size_mult": size_mult,
            "cell": key.as_str(),
            "side": key.side,
            "trades": v.get("trades", 0),
            "reason": v.get("reason"),
        }

    def report(self, *, top_n: int = 12, phase2_enabled: bool = False) -> dict:
        rows = []
        for k, s in self.cells.items():
            if s.trades <= 0 and s.evals <= 0:
                continue
            parts = k.split("|")
            key = CellKey.from_str(k)
            v = self.cell_verdict(key)
            rows.append({
                "cell": k, "asset": key.asset, "horizon": key.horizon, "side": key.side,
                "minute_band": key.minute_band,
                "regime": key.regime, "tv_pattern": key.tv_pattern, "ask_band": key.ask_band,
                "evals": s.evals, "trades": s.trades, "wins": s.wins,
                "win_rate": round(s.wins / s.trades, 4) if s.trades else None,
                "pnl_usd": round(s.pnl_usd, 4),
                "wilson_lo": v.get("wilson_lo"), "wilson_hi": v.get("wilson_hi"),
                "verdict": v.get("verdict"), "reason": v.get("reason"),
            })
        rows.sort(key=lambda r: (-(r["trades"] or 0), -(r["evals"] or 0)))
        follow = [r for r in rows if r.get("verdict") == "promote_follow"][:top_n]
        fade = [r for r in rows if r.get("verdict") == "promote_fade"][:top_n]
        return {
            "observe_only": not phase2_enabled,
            "affects_trading": bool(phase2_enabled),
            "phase": 2 if phase2_enabled else 1,
            "phase2_posterior": bool(phase2_enabled),
            "study_series": list(PHASE1_SERIES_SLUGS),
            "min_samples": self.min_samples,
            "total_cells": len(self.cells),
            "cells_with_trades": sum(1 for s in self.cells.values() if s.trades > 0),
            "total_evals": sum(s.evals for s in self.cells.values()),
            "total_trades": sum(s.trades for s in self.cells.values()),
            "pending_windows": len(self._pending),
            "top_cells": rows[:top_n],
            "follow_candidates": follow,
            "fade_candidates": fade,
            "note": (
                "Phase 2 cell posterior active — Wilson FOLLOW/FADE nudges tier log-odds + size "
                "on directional lane only."
                if phase2_enabled
                else ("Phase 1 cell learning — observe only. Cells accumulate evals every tier tick "
                      "and grade on settlement.")),
        }

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "schema": "directional_cell_learning/2.0",
                "cells": {k: v.to_dict() for k, v in self.cells.items()},
            }, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.exception("cell learning save failed")

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("schema") != "directional_cell_learning/2.0":
                self.cells = {}
                return
            self.cells = {k: CellStats.from_dict(v) for k, v in (data.get("cells") or {}).items()}
        except Exception:  # noqa: BLE001
            logger.exception("cell learning load failed")

    def to_state(self) -> dict:
        return {"cells": {k: v.to_dict() for k, v in self.cells.items()}}

    def load_state(self, data: dict) -> None:
        self.cells = {k: CellStats.from_dict(v) for k, v in (data or {}).get("cells", {}).items()}

    def merge_cells_dict(self, other: dict) -> int:
        """Additive merge of another cells dict. Returns number of keys touched."""
        other_cells = (other or {}).get("cells") or other or {}
        touched = 0
        for key, stats in other_cells.items():
            if not isinstance(stats, dict):
                continue
            cur = self.cells.get(key) or CellStats()
            self.cells[key] = CellStats(
                evals=int(cur.evals) + int(stats.get("evals", 0) or 0),
                trades=int(cur.trades) + int(stats.get("trades", 0) or 0),
                wins=int(cur.wins) + int(stats.get("wins", 0) or 0),
                pnl_usd=float(cur.pnl_usd) + float(stats.get("pnl_usd", 0) or 0),
            )
            touched += 1
        return touched

    def merge_from_disk(self) -> int:
        """Fold /data/directional_cell_learning.json into in-memory cells (offline import)."""
        if self.path is None or not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            logger.exception("cell learning merge_from_disk read failed")
            return 0
        if data.get("schema") not in (None, "directional_cell_learning/2.0"):
            return 0
        # Only add keys / counts that are richer than in-memory (avoid double-count on restart).
        # Strategy: for each disk key, if in-memory trades < disk trades, take disk row as floor.
        disk_cells = data.get("cells") or {}
        touched = 0
        for key, stats in disk_cells.items():
            if not isinstance(stats, dict):
                continue
            disk_trades = int(stats.get("trades", 0) or 0)
            cur = self.cells.get(key)
            if cur is None or int(cur.trades) < disk_trades:
                self.cells[key] = CellStats.from_dict(stats)
                touched += 1
        if touched and self.path is not None:
            self.save()
        return touched


def apply_phase2_to_tier_decision(td, adj: dict, *, ask_up, ask_down, down_only: bool = False):
    """Apply a Phase-2 cell posterior nudge to a :class:`TierDecision` (mutates in place)."""
    if td is None or not adj or not adj.get("enabled"):
        return td
    shift = float(adj.get("logit_shift") or 0.0)
    mult = float(adj.get("size_mult") or 1.0)
    if abs(shift) < 1e-9 and abs(mult - 1.0) < 1e-9:
        return td
    p_up = sigmoid(logit(float(td.p_up)) + shift)
    e_up = (p_up - float(ask_up)) if ask_up is not None else None
    e_dn = ((1.0 - p_up) - float(ask_down)) if ask_down is not None else None
    if down_only:
        side, edge = "down", (e_dn if e_dn is not None else -1.0)
    else:
        cand = [(s, e) for s, e in (("up", e_up), ("down", e_dn)) if e is not None]
        if cand:
            side, edge = max(cand, key=lambda t: t[1])
        else:
            side, edge = td.side, float(td.edge)
    p_chosen = p_up if side == "up" else (1.0 - p_up)
    conviction = abs(2.0 * p_up - 1.0)
    size = round(max(0.0, float(td.size_usd) * mult), 2)
    bd = dict(td.breakdown or {})
    bd["cell_phase2"] = {
        "verdict": adj.get("verdict"),
        "logit_shift": round(shift, 4),
        "size_mult": round(mult, 3),
        "cell": adj.get("cell"),
        "trades": adj.get("trades"),
    }
    td.p_up = p_up
    td.side = side
    td.edge = float(edge)
    td.conviction = conviction
    td.size_usd = size
    td.breakdown = bd
    td.reason = "%s|cell_%s" % (td.reason, adj.get("verdict") or OBSERVE)
    return td
