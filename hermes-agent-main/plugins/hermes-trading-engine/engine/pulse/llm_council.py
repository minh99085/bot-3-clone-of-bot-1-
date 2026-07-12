"""LLM council for the BTC pulse directional decision (PAPER ONLY).

Replaces the old strict gate chain (Grok proposes -> Claude only vetoes) with an evidence-weighted
ENSEMBLE that actually uses both LLMs' compute. Each member contributes an independent directional
view -- ``p_up`` in [0, 1] = P(BTC closes >= open) -- and the council blends them into ONE consensus
weighted by each member's OWN live, graded accuracy (Wilson lower bound over settled windows):

* quant baseline (digital fair value + learned-edge blend),
* Grok decider (its per-window p_up view),
* Claude second-opinion (a directional lean, not just a veto).

Anti-predictive members are automatically driven to a weight floor (so a member that has proven it
is worse than a coin flip stops moving the consensus); members with proven out-of-sample edge
dominate. The council LEARNS who to trust from real outcomes.

Two invariants (paper-only bot):
* The council only PROPOSES a side + confidence. The deterministic execution floor (calibration,
  selectivity, execution-quality EV gate, risk caps, freshness) still decides whether a paper fill
  happens -- the council can never bypass it.
* FAIL-OPEN. If not enough member views are available it returns ``trade=False`` with no side and the
  engine falls back to the quant baseline. It never blocks trading the way a fail-closed gate does.
"""

from __future__ import annotations

import math
import threading
from typing import Optional


def _wilson_lower(correct: int, n: int, z: float = 1.64) -> Optional[float]:
    """One-sided lower Wilson bound of accuracy -- statistically confident floor on a member's
    directional hit-rate (not just a small-sample fluke)."""
    if n <= 0:
        return None
    phat = correct / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4 * n)) / n)) / denom
    return max(0.0, center - margin)


def _wilson_upper(correct: int, n: int, z: float = 1.64) -> Optional[float]:
    """One-sided upper Wilson bound of accuracy -- flags a member as PROVEN anti-predictive when the
    upper bound is still below 0.5 (worse than a coin flip => worth FADING, not just ignoring)."""
    if n <= 0:
        return None
    phat = correct / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4 * n)) / n)) / denom
    return min(1.0, center + margin)


def member_stance(correct: int, n: int, *, prior: float, floor: float = 0.1,
                  min_samples: int = 20, scale: float = 8.0,
                  fade_min_samples: Optional[int] = None):
    """FOLLOW / FADE / IGNORE / COLD decision for one member from its graded accuracy.

    * COLD   (n < min_samples): trust the prior, use the view as-is.
    * FOLLOW (Wilson lower > 0.5): proven predictive -> weight up, use the view as-is.
    * FADE   (Wilson upper < 0.5 AND n >= fade_min_samples): proven anti-predictive -> weight by
      how-wrong, and INVERT the view. Fading needs MORE evidence than following (inverting a signal
      on a small-sample fluke is the "forecast-combination puzzle" noise trap), so fade_min_samples
      defaults higher than min_samples.
    * IGNORE (spans 0.5, or anti-predictive but not yet enough samples to fade): floor, view as-is.

    Returns ``(stance, weight, invert)``."""
    if n < int(min_samples):
        return "cold", max(float(floor), float(prior)), False
    fade_n = int(fade_min_samples) if fade_min_samples is not None else int(min_samples)
    lo = _wilson_lower(correct, n)
    up = _wilson_upper(correct, n)
    if lo is not None and lo > 0.5:
        return "follow", round(float(floor) + (lo - 0.5) * float(scale), 6), False
    if up is not None and up < 0.5 and n >= fade_n:
        return "fade", round(float(floor) + (0.5 - up) * float(scale), 6), True
    return "ignore", float(floor), False


def member_weight(correct: int, n: int, *, prior: float, floor: float = 0.1,
                  min_samples: int = 20, scale: float = 8.0) -> float:
    """Weight for one member from its graded accuracy. Cold (n < min_samples) -> ``prior``. Warm ->
    ``floor + max(0, wilson_lower - 0.5) * scale`` so only PROVEN out-of-sample edge earns weight and
    anti-predictive members collapse to the floor."""
    if n < int(min_samples):
        return max(float(floor), float(prior))
    lo = _wilson_lower(correct, n)
    edge = max(0.0, (lo - 0.5)) if lo is not None else 0.0
    return round(float(floor) + edge * float(scale), 6)


def council_consensus(votes: list, *, min_agreement: float = 0.60,
                      min_margin: float = 0.02, min_members: int = 2) -> dict:
    """Blend member votes into one consensus decision.

    ``votes``: list of ``{"name", "p_up" (0-1), "weight" (>0)}`` -- only members with a usable p_up.
    Trades only when the weighted consensus has enough directional margin AND enough of the weight
    agrees with the consensus side. Pure + deterministic (fully unit-testable)."""
    usable = [v for v in votes
              if v.get("p_up") is not None and float(v.get("weight") or 0.0) > 0.0]
    if len(usable) < int(min_members):
        return {"trade": False, "side": None, "consensus_p_up": None, "reason": "insufficient_members",
                "n_members": len(usable), "agreement": None, "margin": None, "confidence": 0.0,
                "members": [v.get("name") for v in usable]}
    tw = sum(float(v["weight"]) for v in usable)
    cp = sum(float(v["weight"]) * float(v["p_up"]) for v in usable) / tw
    side = "up" if cp >= 0.5 else "down"
    agree_w = sum(float(v["weight"]) for v in usable
                  if (float(v["p_up"]) >= 0.5) == (cp >= 0.5)) / tw
    margin = abs(cp - 0.5)
    trade = (margin >= float(min_margin)) and (agree_w >= float(min_agreement))
    confidence = round(min(1.0, margin * 2.0) * agree_w, 4)
    return {
        "trade": bool(trade), "side": side, "consensus_p_up": round(cp, 4),
        "agreement": round(agree_w, 4), "margin": round(margin, 4), "confidence": confidence,
        "n_members": len(usable),
        "members": {v["name"]: {"p_up": round(float(v["p_up"]), 4),
                                "weight": round(float(v["weight"]), 4)} for v in usable},
        "reason": ("consensus_%s" % side) if trade else (
            "low_margin" if margin < float(min_margin) else "low_agreement"),
    }


def best_ev_side(p_up, up_ask, down_ask, *, min_edge: float = 0.0):
    """Pick the side with the highest per-$1 EV = P(side) - ask, given a consensus ``p_up`` and the
    two ask prices. Returns ``(side, ev)`` for the best side IF it clears ``min_edge``, else
    ``(None, ev)``. Unlike favorite-by-probability, this takes the CHEAP underdog when it is
    underpriced (high reward/risk, clears the price cap) and refuses to overpay for the favorite."""
    if p_up is None:
        return None, None
    p_up = float(p_up)
    evs = []
    if up_ask is not None:
        evs.append(("up", p_up - float(up_ask)))
    if down_ask is not None:
        evs.append(("down", (1.0 - p_up) - float(down_ask)))
    if not evs:
        return None, None
    side, ev = max(evs, key=lambda z: z[1])
    return (side if ev >= float(min_edge) else None), round(ev, 4)


class LLMCouncil:
    """Stateful council: holds each member's graded accuracy, derives live weights, produces the
    per-window consensus, and grades members vs realized outcomes. Thread-safe; restart-safe via
    ``to_state``/``load_state``. PAPER ONLY."""

    #: cold-start priors. ONLY the quant anchor carries real cold weight (it is the deterministic,
    #: calibrated fair value). Every other member (LLMs, TV) must EARN weight from graded accuracy --
    #: until then it sits at the floor and its view is shrunk toward neutral, so an ungraded member
    #: can never swing the vote on an arbitrary prior. (Forecast-combination puzzle: don't trust
    #: estimated/assumed weights you can't back with out-of-sample evidence.)
    DEFAULT_PRIORS = {"quant": 1.0}

    def _prior(self, name: str) -> float:
        if name in self.priors:
            return self.priors[name]
        return float(self.weight_floor)

    def __init__(self, *, enabled: bool = False, min_agreement: float = 0.60,
                 min_margin: float = 0.02, min_members: int = 2, min_samples: int = 20,
                 weight_floor: float = 0.1, weight_scale: float = 8.0,
                 priors: Optional[dict] = None, anchor: str = "quant",
                 fade_min_samples: Optional[int] = None):
        self.enabled = bool(enabled)
        self.min_agreement = float(min_agreement)
        self.min_margin = float(min_margin)
        self.min_members = int(min_members)
        self.min_samples = int(min_samples)
        self.weight_floor = float(weight_floor)
        self.weight_scale = float(weight_scale)
        self.priors = dict(priors or self.DEFAULT_PRIORS)
        self.anchor = str(anchor)
        # fading (inverting a member) needs more evidence than following -- guards the small-sample
        # noise trap; defaults to max(min_samples, 30).
        self.fade_min_samples = int(fade_min_samples) if fade_min_samples is not None \
            else max(int(min_samples), 30)
        self._lock = threading.Lock()
        self._stats: dict = {}          # name -> {"n","correct"}
        self.ignore_members: set = set()  # retired members: never vote, grade, or report
        self.reset_token = ""           # token of the last applied one-time member reset
        self.decisions = 0              # consensus dicts produced with trade=True
        self.evaluations = 0           # decide() calls
        self.graded = 0

    def _is_cold(self, name: str) -> bool:
        return int((self._stats.get(name) or {}).get("n", 0) or 0) < self.min_samples

    def reset_members(self, names) -> int:
        """Clear the graded stats for the named members WITHOUT retiring them -- they keep voting and
        grade FRESH. Use when a member's underlying signal changes meaning (e.g. a 5m chart switched
        from a trend alert to a mean-reversion alert), so its old grades no longer describe it."""
        with self._lock:
            n = 0
            for name in (names or ()):
                if str(name) in self._stats:
                    del self._stats[str(name)]
                    n += 1
            return n

    def maybe_reset(self, token, names) -> bool:
        """One-time, token-gated reset: if ``token`` is set and differs from the last applied token,
        reset ``names`` and record the token (so it does not re-run on later restarts). Returns True
        if a reset was applied."""
        token = str(token or "")
        if not token or token == self.reset_token:
            return False
        self.reset_members(names)
        with self._lock:
            self.reset_token = token
        return True

    def forget(self, names) -> None:
        """Retire members (e.g. TFs the operator removed): drop their graded stats and ignore any
        future view/grade for them so they vanish from the council + report. Idempotent."""
        with self._lock:
            for n in (names or ()):
                self.ignore_members.add(str(n))
            for n in list(self._stats):
                if n in self.ignore_members:
                    del self._stats[n]

    def _weight_locked(self, name: str) -> float:
        s = self._stats.get(name) or {"n": 0, "correct": 0}
        # unproven (cold) non-anchor members get the floor, NOT their prior -- they must earn weight
        if s["n"] < self.min_samples and name != self.anchor:
            return self.weight_floor
        return member_weight(s["correct"], s["n"], prior=self._prior(name),
                             floor=self.weight_floor, min_samples=self.min_samples,
                             scale=self.weight_scale)

    def _stance_locked(self, name: str):
        s = self._stats.get(name) or {"n": 0, "correct": 0}
        stance, weight, invert = member_stance(
            s["correct"], s["n"], prior=self._prior(name), floor=self.weight_floor,
            min_samples=self.min_samples, scale=self.weight_scale,
            fade_min_samples=self.fade_min_samples)
        if s["n"] < self.min_samples and name != self.anchor:
            weight = self.weight_floor      # cold non-anchor: floor weight (view is also shrunk)
        return stance, weight, invert

    def decide(self, views: dict, *, min_agreement: Optional[float] = None,
               min_margin: Optional[float] = None) -> dict:
        """``views``: ``{member_name: p_up_or_None}``. Each member's view is FOLLOWED, FADED (inverted),
        or IGNORED based on its live graded accuracy, then blended into the consensus."""
        eff_agreement = float(min_agreement) if min_agreement is not None else self.min_agreement
        eff_margin = float(min_margin) if min_margin is not None else self.min_margin
        with self._lock:
            self.evaluations += 1
            votes = []
            stances = {}
            for n, p in (views or {}).items():
                if p is None or n in self.ignore_members:
                    continue
                stance, weight, invert = self._stance_locked(n)
                eff = (1.0 - float(p)) if invert else float(p)
                # shrink an unproven non-anchor member's view toward neutral (0.5) proportional to how
                # little it has been graded, so a cold member can't swing the vote on a raw guess.
                sn = int((self._stats.get(n) or {}).get("n", 0) or 0)
                if sn < self.min_samples and n != self.anchor:
                    k = sn / float(self.min_samples) if self.min_samples else 0.0
                    eff = 0.5 + (eff - 0.5) * k
                votes.append({"name": n, "p_up": eff, "weight": weight})
                stances[n] = {"stance": stance, "weight": round(weight, 4),
                              "raw_p_up": round(float(p), 4), "effective_p_up": round(eff, 4)}
            out = council_consensus(votes, min_agreement=eff_agreement,
                                    min_margin=eff_margin, min_members=self.min_members)
            out["stances"] = stances
            if out.get("trade"):
                self.decisions += 1
            return out

    def grade(self, views: dict, outcome_up: bool) -> None:
        """Grade each member's directional view (p_up vs realized close). ``views`` is the snapshot
        captured at decision time. Restart-safe (engine persists the snapshot in its pending list)."""
        with self._lock:
            up = bool(outcome_up)
            for name, p in (views or {}).items():
                if p is None or name in self.ignore_members:
                    continue
                s = self._stats.setdefault(name, {"n": 0, "correct": 0})
                s["n"] += 1
                s["correct"] += int((float(p) >= 0.5) == up)
            self.graded += 1

    def report(self) -> dict:
        with self._lock:
            members = {}
            for name in set(list(self.priors) + list(self._stats)):
                if name in self.ignore_members:
                    continue
                s = self._stats.get(name) or {"n": 0, "correct": 0}
                acc = round(s["correct"] / s["n"], 4) if s["n"] else None
                stance, weight, invert = self._stance_locked(name)
                members[name] = {"n": s["n"], "accuracy": acc,
                                 "accuracy_lower_ci": (round(_wilson_lower(s["correct"], s["n"]), 4)
                                                       if s["n"] else None),
                                 "accuracy_upper_ci": (round(_wilson_upper(s["correct"], s["n"]), 4)
                                                       if s["n"] else None),
                                 "stance": stance, "faded": invert, "weight": round(weight, 4),
                                 "prior": self._prior(name)}
            return {
                "enabled": self.enabled, "paper_only": True,
                "affects_trading": self.enabled,
                "min_agreement": self.min_agreement, "min_margin": self.min_margin,
                "min_members": self.min_members, "min_samples": self.min_samples,
                "evaluations": self.evaluations, "trade_decisions": self.decisions,
                "graded": self.graded, "members": members,
                "note": ("evidence-weighted ensemble of quant + Grok + Claude directional views; "
                         "members weighted by live Wilson-lower accuracy (anti-predictive -> floor); "
                         "proposes side only, execution floor still authoritative; fail-open to "
                         "baseline. PAPER ONLY."),
            }

    def to_state(self) -> dict:
        with self._lock:
            return {"stats": {n: dict(s) for n, s in self._stats.items()},
                    "decisions": self.decisions, "evaluations": self.evaluations,
                    "graded": self.graded, "reset_token": self.reset_token}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.reset_token = str(data.get("reset_token") or "")
        with self._lock:
            self._stats = {n: {"n": int(s.get("n", 0) or 0), "correct": int(s.get("correct", 0) or 0)}
                           for n, s in (data.get("stats") or {}).items()
                           if n not in self.ignore_members}
            self.decisions = int(data.get("decisions", 0) or 0)
            self.evaluations = int(data.get("evaluations", 0) or 0)
            self.graded = int(data.get("graded", 0) or 0)
