"""PRISM Phase 1 — Bayesian belief engine (PAPER ONLY).

Update ``P(UP)`` for a Polymarket crypto up/down window from many signals in **log-odds** space,
not additive point scores. Each signal contributes an independent evidence shift::

    posterior_log_odds = prior_log_odds + sum_i  signal_log_odds_shift(obs_i)

A raw directional signal (tv_15m, cex_lead, book_imbalance, quant_fair, chainlink_anchor)
contributes ``direction * weight * strength * freshness * SIGNAL_UNIT_LOG_ODDS``. A *named pattern
condition* whose key lives in the likelihood-ratio (LR) table (e.g. ``tv_conflict``,
``stale_polymarket_down``) contributes ``direction * strength * freshness * log(LR)`` — an LR > 1
confirms the proposed side, an LR < 1 fades it (pulls the posterior back toward 0.5 / the other
side). The LR table is the learnable parameter set, loaded from and saved to disk so nightly
recalibration can grade it on real settled outcomes.

Observe-only in Phase 1: this module is math + tests, not wired into the engine.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pulse.prism.belief")

# Probability clamp so logit/sigmoid never blow up at the 0/1 boundary.
_EPS = 1e-6

# A full-strength, fully-fresh, unit-weight raw directional signal is treated as a 3:1 likelihood
# ratio of evidence. Real signal weights are < 1, so effective per-signal shifts stay modest.
SIGNAL_UNIT_LOG_ODDS = math.log(3.0)

# Per-signal weight + freshness half-life (seconds). half_life_s = None means "never decays".
# Consumed here for raw directional shifts and re-exported for the Phase 2 information tracker.
DEFAULT_SIGNAL_WEIGHTS: dict[str, dict[str, Optional[float]]] = {
    "chainlink_anchor": {"weight": 0.05, "half_life_s": None},
    "cex_lead": {"weight": 0.20, "half_life_s": 45.0},
    "book_imbalance": {"weight": 0.15, "half_life_s": 30.0},
    "tv_15m": {"weight": 0.18, "half_life_s": 720.0},
    "tv_30m": {"weight": 0.15, "half_life_s": 1500.0},
    "tv_45m": {"weight": 0.12, "half_life_s": 2700.0},
    "tv_60m": {"weight": 0.12, "half_life_s": 3600.0},
    "tv_240m": {"weight": 0.10, "half_life_s": 14400.0},
    "tv_1440m": {"weight": 0.08, "half_life_s": 86400.0},
    "quant_fair": {"weight": 0.15, "half_life_s": 60.0},
}

# Initial likelihood ratios for named pattern conditions. Overridable from disk (learnable).
# LR = P(evidence | UP) / P(evidence | DOWN) for a *confirming* observation of the proposed side:
#   > 1 confirms the proposed side, < 1 fades it.
DEFAULT_LIKELIHOOD_RATIOS: dict[str, float] = {
    "tv_15_30_agree": 1.35,
    "tv_conflict": 0.55,
    "tv_strong_same_side": 0.70,   # contrarian fade of a strong same-side print
    "tv_weak_aligned": 1.15,
    "cex_stale_book_up": 1.45,
    "stale_polymarket_down": 0.40,
    "liquidity_danger_tight": 1.25,
    "conviction_lean": 1.50,
    "confidence_medium": 0.75,
}

_LR_TABLE_FILENAME = "prism_lr_table.json"


def logit(p: float) -> float:
    """Log-odds of probability ``p``, clamped away from the 0/1 boundary."""
    q = min(max(float(p), _EPS), 1.0 - _EPS)
    return math.log(q / (1.0 - q))


def sigmoid(x: float) -> float:
    """Inverse of :func:`logit`; maps a log-odds value back to a probability in (0, 1)."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def freshness(age_s: Optional[float], half_life_s: Optional[float]) -> float:
    """Exponential freshness decay in [0, 1]. ``half_life_s`` None/inf/<=0-age means fully fresh.

    A signal at exactly one half-life old contributes 0.5; two half-lives 0.25; and so on.
    """
    if half_life_s is None or (isinstance(half_life_s, float) and math.isinf(half_life_s)):
        return 1.0
    try:
        hl = float(half_life_s)
        age = float(age_s) if age_s is not None else 0.0
    except (TypeError, ValueError):
        return 1.0
    if hl <= 0:
        return 1.0 if age <= 0 else 0.0
    if age <= 0:
        return 1.0
    decay = math.exp(-math.log(2.0) * (age / hl))
    return max(0.0, min(1.0, decay))


@dataclass
class SignalObservation:
    """One piece of evidence about ``P(UP)`` for a window.

    direction: +1 (points UP), -1 (points DOWN), 0 (neutral / no signal).
    strength:  [0, 1] confidence magnitude of the signal.
    age_s:     seconds since the signal was observed (drives freshness decay).
    half_life_s: freshness half-life; None means the signal never decays.
    weight:    relative importance for *raw directional* signals (ignored for LR-table patterns,
               which carry their own magnitude via the likelihood ratio).
    """

    name: str
    direction: int = 0
    strength: float = 0.0
    age_s: float = 0.0
    half_life_s: Optional[float] = None
    weight: float = 1.0

    def clamped_direction(self) -> int:
        if self.direction > 0:
            return 1
        if self.direction < 0:
            return -1
        return 0


@dataclass
class BeliefState:
    """Result of a belief update."""

    posterior_p: float
    log_odds: float
    n_signals_used: int
    breakdown: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "posterior_p": round(self.posterior_p, 6),
            "log_odds": round(self.log_odds, 6),
            "n_signals_used": int(self.n_signals_used),
            "breakdown": list(self.breakdown),
        }


def make_observation(name: str, direction: int, strength: float, age_s: float = 0.0,
                     *, weights: Optional[dict] = None) -> SignalObservation:
    """Build a raw directional :class:`SignalObservation`, pulling weight + half-life from the
    signal-weight table (defaults to :data:`DEFAULT_SIGNAL_WEIGHTS`)."""
    table = weights if weights is not None else DEFAULT_SIGNAL_WEIGHTS
    spec = table.get(name, {})
    return SignalObservation(
        name=name,
        direction=int(direction),
        strength=float(strength),
        age_s=float(age_s),
        half_life_s=spec.get("half_life_s"),
        weight=float(spec.get("weight", 1.0)),
    )


def signal_log_odds_shift(obs: SignalObservation, lr_table: Optional[dict] = None) -> float:
    """Log-odds contribution of a single observation.

    Named pattern conditions (``obs.name`` present in ``lr_table``) use ``log(LR)`` as their
    per-unit magnitude; raw directional signals use :data:`SIGNAL_UNIT_LOG_ODDS` scaled by weight.
    Both are scaled by direction, strength, and freshness. A neutral, stale, or zero-strength
    observation contributes nothing.
    """
    lr_table = lr_table if lr_table is not None else DEFAULT_LIKELIHOOD_RATIOS
    direction = obs.clamped_direction()
    strength = max(0.0, min(1.0, float(obs.strength or 0.0)))
    if direction == 0 or strength <= 0.0:
        return 0.0
    fresh = freshness(obs.age_s, obs.half_life_s)
    if fresh <= 0.0:
        return 0.0
    if obs.name in lr_table:
        lr = float(lr_table[obs.name])
        unit = math.log(max(lr, _EPS))            # LR<1 -> negative (fades proposed side)
        return direction * strength * fresh * unit
    weight = max(0.0, float(obs.weight or 0.0))
    return direction * weight * strength * fresh * SIGNAL_UNIT_LOG_ODDS


def update_belief(prior_p: float, observations: list,
                  lr_table: Optional[dict] = None) -> BeliefState:
    """Bayesian log-odds update of ``prior_p`` given a list of :class:`SignalObservation`.

    Empty observations return the (clamped) prior unchanged. Prior 0/1 is clamped so the update
    is always well defined.
    """
    lr_table = lr_table if lr_table is not None else DEFAULT_LIKELIHOOD_RATIOS
    log_odds = logit(prior_p)
    breakdown: list = []
    used = 0
    for obs in observations or []:
        shift = signal_log_odds_shift(obs, lr_table)
        if shift != 0.0:
            used += 1
        log_odds += shift
        breakdown.append({
            "name": obs.name,
            "direction": obs.clamped_direction(),
            "strength": round(float(obs.strength or 0.0), 4),
            "freshness": round(freshness(obs.age_s, obs.half_life_s), 4),
            "shift": round(shift, 6),
        })
    return BeliefState(
        posterior_p=sigmoid(log_odds),
        log_odds=log_odds,
        n_signals_used=used,
        breakdown=breakdown,
    )


class BeliefEngine:
    """Stateful belief engine: owns a learnable LR table (disk-bound) and records outcomes.

    Phase 1 keeps :meth:`record_outcome` a logging stub; a later phase turns the accumulated
    counts into a nightly LR recalibration. PAPER ONLY.
    """

    def __init__(self, data_dir: Optional[Path] = None,
                 lr_table: Optional[dict] = None):
        self.data_dir = Path(data_dir) if data_dir else None
        self.lr_table: dict[str, float] = dict(DEFAULT_LIKELIHOOD_RATIOS)
        if lr_table:
            self.lr_table.update({str(k): float(v) for k, v in lr_table.items()})
        # signal_key -> {"wins": int, "losses": int} for nightly recalibration (P1: log only).
        self.outcomes: dict[str, dict[str, int]] = {}
        if self.data_dir is not None:
            self.load()

    @property
    def lr_path(self) -> Optional[Path]:
        return (self.data_dir / _LR_TABLE_FILENAME) if self.data_dir is not None else None

    def load(self) -> None:
        """Merge a disk LR table (and outcome counts) over the defaults. Tolerates missing/corrupt."""
        path = self.lr_path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — never break startup on a bad file
            logger.warning("prism belief: could not read %s; using defaults", path)
            return
        for k, v in (data.get("lr_table") or {}).items():
            try:
                self.lr_table[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        for k, v in (data.get("outcomes") or {}).items():
            if isinstance(v, dict):
                self.outcomes[str(k)] = {
                    "wins": int(v.get("wins", 0) or 0),
                    "losses": int(v.get("losses", 0) or 0),
                }

    def save(self) -> None:
        """Persist the current LR table + outcome counts. No-op without a data_dir."""
        path = self.lr_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "schema": "prism_lr_table/1.0",
                "lr_table": self.lr_table,
                "outcomes": self.outcomes,
            }, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001 — persistence must never crash the loop
            logger.warning("prism belief: could not write %s", path)

    def update(self, prior_p: float, observations: list) -> BeliefState:
        """Belief update using this engine's (possibly disk-loaded) LR table."""
        return update_belief(prior_p, observations, self.lr_table)

    def record_outcome(self, signal_key: str, won: bool) -> None:
        """Accumulate a settled outcome for a signal/pattern key (nightly recalibration input).

        Phase 1 stub: tally + log only. A later phase converts these counts into updated LRs.
        """
        rec = self.outcomes.setdefault(str(signal_key), {"wins": 0, "losses": 0})
        if won:
            rec["wins"] += 1
        else:
            rec["losses"] += 1
        logger.debug("prism belief outcome %s won=%s -> %s", signal_key, won, rec)

    def report(self) -> dict:
        """Observe-only summary for the status API / dashboard."""
        return {
            "enabled": True,
            "lr_table_size": len(self.lr_table),
            "outcomes_tracked": len(self.outcomes),
            "lr_path": str(self.lr_path) if self.lr_path else None,
        }
