"""PRISM Phase 4 — Monte-Carlo ensemble edge E and confidence C (PAPER ONLY).

Compute the edge ``E`` and MC confidence ``C`` for a directional candidate from a 4-model ensemble.
We **reuse** ``engine.pulse.monte_carlo`` (no duplicated GBM math). MC earns its keep over the
closed form only where the Gaussian assumption is wrong: TV/CEX drift, fat-tail jumps under
illiquidity, and regime-scaled vol/drift. Model disagreement becomes the confidence signal::

    M1  closed-form digital P(up)                          (analytic reference)
    M2  MC with informed drift (TV + CEX)                  weight 0.30
    M3  MC with jumps when liquidity is dangerous, else M2 weight 0.20
    M4  MC with regime mu/sigma multipliers (trend/chop)   weight 0.25
    M1                                                     weight 0.25

    p_up_mean = Σ w_i p_i ;  p_up_std = sqrt(Σ w_i (p_i-mean)^2)
    C = 1 - min(1, p_up_std / STD_FULL)                    (models agree -> C~1)
    ev_up = p_up_mean - ask_up ;  ev_down = (1-p_up_mean) - ask_down
    E = ev(chosen side) - slippage_buffer

Graceful fallback when numpy is absent: E from the closed form only, C = 0.5. Observe-only — this
never authorizes a fill; it feeds the PRISM rank R = I * max(0,E) * C used by the stopping gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from engine.pulse.monte_carlo import (
    HAVE_NUMPY,
    closed_form_digital_p_up,
    mc_digital_p_up,
)

# Weighted std at which the ensemble is treated as maximally uncertain (C -> 0).
_STD_FULL = 0.15

# Ensemble model weights: [M1 closed-form, M2 informed-drift, M3 jumps, M4 regime].
_WEIGHTS = (0.25, 0.30, 0.20, 0.25)

# Markov regime -> (drift multiplier, sigma multiplier). Trending amplifies drift; chop widens vol
# and damps drift; stale-polymarket states are mildly cautious.
_REGIME_MULT: dict[str, tuple] = {
    "trending": (1.3, 1.0),
    "chop_noise": (0.5, 1.2),
    "chop": (0.5, 1.2),
    "mean_reverting": (0.6, 1.1),
    "stale_polymarket_up": (0.8, 1.1),
    "stale_polymarket_down": (0.8, 1.1),
}

_DANGER_STATES = {"liquidation_spike", "stale_polymarket_down", "danger"}


@dataclass
class EnsembleInput:
    s_now: float
    s_open: float
    sigma_per_sec: float
    ttc_s: float
    ask_up: Optional[float] = None
    ask_down: Optional[float] = None
    side: Optional[str] = None                 # "up" | "down" | None (choose best EV)
    tv_score_normalized: float = 0.0           # [-1, 1], + = up
    cex_drift_bps: float = 0.0
    markov_state: Optional[str] = None
    liquidity_danger: bool = False
    slippage_buffer: float = 0.01


@dataclass
class EnsembleResult:
    p_up_mean: float
    p_up_std: float
    ev_up: Optional[float]
    ev_down: Optional[float]
    E: float
    C: float
    side: Optional[str]
    used_numpy: bool
    models: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "p_up_mean": round(self.p_up_mean, 5),
            "p_up_std": round(self.p_up_std, 5),
            "ev_up": (round(self.ev_up, 5) if self.ev_up is not None else None),
            "ev_down": (round(self.ev_down, 5) if self.ev_down is not None else None),
            "E": round(self.E, 5),
            "C": round(self.C, 5),
            "side": self.side,
            "used_numpy": self.used_numpy,
            "models": {k: round(v, 5) for k, v in self.models.items()},
        }


def tv_drift_mu(tv_score: float, sigma_per_sec: float, *, scale: float = 0.30,
                horizon_s: float = 3600.0) -> float:
    """Per-second drift implied by a normalized TV score, capped at ``scale*sigma/sqrt(horizon)``.

    A full-conviction score (|tv_score|=1) shifts the hourly mean by ~``scale`` standard deviations.
    """
    if sigma_per_sec is None or sigma_per_sec <= 0:
        return 0.0
    score = max(-1.0, min(1.0, float(tv_score or 0.0)))
    cap = float(scale) * float(sigma_per_sec) / math.sqrt(max(1.0, horizon_s))
    return score * cap


def cex_drift_mu(bps: float, *, cap_bps: float = 50.0, horizon_s: float = 3600.0) -> float:
    """Per-second drift from an expected CEX-lead move in basis points over the horizon (bounded)."""
    try:
        b = float(bps or 0.0)
    except (TypeError, ValueError):
        return 0.0
    b = max(-cap_bps, min(cap_bps, b))
    return (b / 1e4) / max(1.0, horizon_s)


def _regime_mult(markov_state: Optional[str]) -> tuple:
    return _REGIME_MULT.get(str(markov_state or ""), (1.0, 1.0))


def _is_danger(inp: EnsembleInput) -> bool:
    if inp.liquidity_danger:
        return True
    return str(inp.markov_state or "") in _DANGER_STATES


def _weighted_mean_std(vals: list, weights: list) -> tuple:
    wsum = sum(weights)
    if wsum <= 0:
        return (0.5, 0.0)
    mean = sum(v * w for v, w in zip(vals, weights)) / wsum
    var = sum(w * (v - mean) ** 2 for v, w in zip(vals, weights)) / wsum
    return (mean, math.sqrt(max(0.0, var)))


def run_ensemble(inp: EnsembleInput, *, n_paths: int = 20000, seed: Optional[int] = None,
                 tv_drift_scale: float = 0.30) -> EnsembleResult:
    """Run the 4-model ensemble and return E, C, and the per-model P(up)."""
    sigma = float(inp.sigma_per_sec or 0.0)
    ttc = float(inp.ttc_s or 0.0)
    mu_informed = (tv_drift_mu(inp.tv_score_normalized, sigma, scale=tv_drift_scale)
                   + cex_drift_mu(inp.cex_drift_bps))

    m1 = closed_form_digital_p_up(inp.s_now, inp.s_open, sigma, ttc, mu_per_sec=0.0)

    if HAVE_NUMPY and sigma > 0 and ttc > 0:
        seed2 = None if seed is None else seed + 1
        seed3 = None if seed is None else seed + 2
        seed4 = None if seed is None else seed + 3
        m2 = mc_digital_p_up(inp.s_now, inp.s_open, sigma, ttc, mu_per_sec=mu_informed,
                             n_paths=n_paths, seed=seed2)
        if _is_danger(inp):
            jump_sigma = max(50.0 * sigma, 5e-4)
            m3 = mc_digital_p_up(inp.s_now, inp.s_open, sigma, ttc, mu_per_sec=mu_informed,
                                 n_paths=n_paths, seed=seed3,
                                 jump_intensity_per_sec=1.0 / 3600.0, jump_sigma=jump_sigma)
        else:
            m3 = m2
        mu_mult, sig_mult = _regime_mult(inp.markov_state)
        m4 = mc_digital_p_up(inp.s_now, inp.s_open, sigma * sig_mult, ttc,
                             mu_per_sec=mu_informed * mu_mult, n_paths=n_paths, seed=seed4)
        vals = [m1, m2, m3, m4]
        weights = list(_WEIGHTS)
        used_numpy = True
    else:
        vals = [m1]
        weights = [1.0]
        used_numpy = False

    p_up_mean, p_up_std = _weighted_mean_std(vals, weights)
    if not used_numpy:
        C = 0.5
    else:
        C = max(0.0, min(1.0, 1.0 - p_up_std / _STD_FULL))

    ev_up = (p_up_mean - float(inp.ask_up)) if inp.ask_up is not None else None
    ev_down = ((1.0 - p_up_mean) - float(inp.ask_down)) if inp.ask_down is not None else None

    side = (inp.side or "").strip().lower() or None
    if side == "up":
        chosen_ev = ev_up
    elif side == "down":
        chosen_ev = ev_down
    else:
        candidates = [(s, e) for s, e in (("up", ev_up), ("down", ev_down)) if e is not None]
        if candidates:
            side, chosen_ev = max(candidates, key=lambda t: t[1])
        else:
            side, chosen_ev = None, None

    if chosen_ev is None:
        E = -1.0                                  # no tradeable ask on the chosen side
    else:
        E = float(chosen_ev) - float(inp.slippage_buffer)

    models = {"M1_closed_form": m1}
    if used_numpy:
        models.update({"M2_informed_drift": vals[1], "M3_jumps": vals[2], "M4_regime": vals[3]})

    return EnsembleResult(
        p_up_mean=p_up_mean, p_up_std=p_up_std, ev_up=ev_up, ev_down=ev_down,
        E=E, C=C, side=side, used_numpy=used_numpy, models=models)
