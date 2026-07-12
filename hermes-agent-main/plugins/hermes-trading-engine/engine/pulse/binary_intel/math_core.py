"""Binary market quantitative math — invented formulas for Polymarket up/down.

Digital/cash-or-nothing contracts settle Up iff S_close >= S_open (Chainlink).
These closed forms extend the engine's GBM digital fair with:
  * moneyness / displacement z
  * binary theta (time decay of P(up))
  * Shannon entropy of the market mid (uncertainty)
  * information gain from 5m RSI divergence overlays
  * estimation-error Kelly (fractional Kelly haircut by p-uncertainty)
  * convergence edge (model vs market as TTC shrinks)

PAPER ONLY. Restrict-only consumers — never force fills.
"""

from __future__ import annotations

import math
from typing import Optional


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def displacement_z(
    s_now: float,
    s_open: float,
    sigma_per_sec: float,
    ttc_s: float,
) -> Optional[float]:
    """Moneyness in σ√t units: z = ln(S_now/S_open) / (σ√t).

    |z| >> 1 → deep ITM/OTM for the Up contract; |z|≈0 → ATM.
    """
    if s_now is None or s_open is None or s_now <= 0 or s_open <= 0:
        return None
    if sigma_per_sec is None or sigma_per_sec <= 0:
        return None
    t = max(float(ttc_s), 1e-9)
    denom = float(sigma_per_sec) * math.sqrt(t)
    if denom <= 1e-15:
        return None
    return math.log(float(s_now) / float(s_open)) / denom


def binary_d2(
    s_now: float,
    s_open: float,
    sigma_per_sec: float,
    ttc_s: float,
    *,
    mu_per_sec: float = 0.0,
) -> Optional[float]:
    """Black–Scholes-style d₂ for digital Up (strike = window open)."""
    if s_now is None or s_open is None or s_now <= 0 or s_open <= 0:
        return None
    if sigma_per_sec is None or sigma_per_sec <= 0:
        return None
    t = float(ttc_s)
    if t <= 0:
        return None
    sig = float(sigma_per_sec)
    return (math.log(float(s_now) / float(s_open))
            + (mu_per_sec - 0.5 * sig * sig) * t) / (sig * math.sqrt(t))


def binary_theta(
    s_now: float,
    s_open: float,
    sigma_per_sec: float,
    ttc_s: float,
    *,
    mu_per_sec: float = 0.0,
) -> Optional[float]:
    """∂P(Up)/∂t under GBM digital (per second).

    θ = φ(d₂) · ∂d₂/∂t
    Negative θ when deep ITM Up and clock running (P→1 slower than market pricing).
    Used to judge whether waiting burns edge vs sniping now.
    """
    d2 = binary_d2(s_now, s_open, sigma_per_sec, ttc_s, mu_per_sec=mu_per_sec)
    if d2 is None:
        return None
    t = float(ttc_s)
    if t <= 1e-6:
        return 0.0
    sig = float(sigma_per_sec)
    ln_m = math.log(float(s_now) / float(s_open))
    # ∂d₂/∂t = (μ - 0.5σ²)/(2σ√t) - ln(S/K)/(2σ t^{3/2})  ... rearrange:
    # d₂ = [ln + (μ-0.5σ²)t] / (σ√t) = ln/(σ√t) + (μ-0.5σ²)√t / σ
    # ∂d₂/∂t = -ln/(2σ t^{3/2}) + (μ-0.5σ²)/(2σ √t)
    dd2_dt = (-ln_m / (2.0 * sig * (t ** 1.5))
              + (mu_per_sec - 0.5 * sig * sig) / (2.0 * sig * math.sqrt(t)))
    return float(_norm_pdf(d2) * dd2_dt)


def shannon_entropy_bits(p: float) -> float:
    """Binary Shannon entropy H(p) in bits. Max 1.0 at p=0.5."""
    p = _clamp01(p)
    if p <= 1e-12 or p >= 1.0 - 1e-12:
        return 0.0
    return float(-(p * math.log2(p) + (1.0 - p) * math.log2(1.0 - p)))


def market_uncertainty(poly_mid: Optional[float]) -> Optional[dict]:
    """Entropy + certainty score for Polymarket mid."""
    if poly_mid is None:
        return None
    p = _clamp01(float(poly_mid))
    h = shannon_entropy_bits(p)
    return {
        "poly_mid": round(p, 6),
        "entropy_bits": round(h, 6),
        "certainty": round(1.0 - h, 6),  # 0=max uncertainty, 1=resolved-like
        "near_atm": bool(abs(p - 0.5) < 0.08),
    }


def bayes_update_prob(prior: float, *, likelihood_ratio: float) -> float:
    """Odds-form Bayes: posterior = prior_odds * LR → prob."""
    p = _clamp01(prior)
    lr = max(1e-6, float(likelihood_ratio))
    odds = (p / max(1e-12, 1.0 - p)) * lr
    return _clamp01(odds / (1.0 + odds))


def rsi_divergence_lr(*, lean: Optional[str], proposed_side: Optional[str],
                      strength: float = 0.75) -> float:
    """Likelihood ratio for regular RSI divergence confirm/fade.

    Confirm (aligned): LR > 1; fade (opposed): LR < 1; missing: LR = 1.
    Strength scales log-LR toward ±ln(2.2) at full strength.
    """
    lean_l = str(lean or "").lower()
    side_l = str(proposed_side or "").lower()
    if lean_l not in ("up", "down") or side_l not in ("up", "down"):
        return 1.0
    s = _clamp01(float(strength or 0.75))
    log_lr_max = math.log(2.2)  # ~0.788
    if lean_l == side_l:
        return math.exp(s * log_lr_max)
    return math.exp(-s * log_lr_max)


def information_gain_rsi(
    *,
    prior_p_up: Optional[float],
    lean: Optional[str],
    proposed_side: Optional[str],
    strength: float = 0.75,
) -> Optional[dict]:
    """Bits of information RSI divergence adds vs prior."""
    if prior_p_up is None:
        return None
    prior = _clamp01(float(prior_p_up))
    lr = rsi_divergence_lr(lean=lean, proposed_side=proposed_side, strength=strength)
    post = bayes_update_prob(prior, likelihood_ratio=lr)
    h0 = shannon_entropy_bits(prior)
    h1 = shannon_entropy_bits(post)
    return {
        "prior_p_up": round(prior, 6),
        "posterior_p_up": round(post, 6),
        "likelihood_ratio": round(lr, 6),
        "entropy_prior": round(h0, 6),
        "entropy_posterior": round(h1, 6),
        "info_gain_bits": round(max(0.0, h0 - h1), 6),
        "lean": (str(lean).lower() if lean else None),
        "aligned": (str(lean or "").lower() == str(proposed_side or "").lower()
                    if lean and proposed_side else None),
    }


def estimation_error_kelly(
    *,
    p_win: float,
    price: float,
    p_uncertainty: float = 0.0,
    fraction: float = 0.25,
    max_f: float = 0.15,
) -> dict:
    """Fractional Kelly with estimation-error haircut (binary payoff).

    Full Kelly: f* = (p - c) / (1 - c)
    Haircut:    f  = fraction * f* * max(0, 1 - κ·σ_p)
    where σ_p is model uncertainty in probability units.
    """
    p = _clamp01(float(p_win))
    c = _clamp01(float(price))
    if c >= 0.999 or c <= 0.001:
        return {"f_star": 0.0, "f_adj": 0.0, "edge": round(p - c, 6), "haircut": 1.0}
    f_star = (p - c) / (1.0 - c)
    if f_star <= 0:
        return {"f_star": round(f_star, 6), "f_adj": 0.0,
                "edge": round(p - c, 6), "haircut": 1.0}
    unc = max(0.0, float(p_uncertainty))
    # κ≈2: 10% p-uncertainty → 20% size cut; 50% → full cut
    haircut = _clamp01(1.0 - 2.0 * unc)
    f_adj = float(fraction) * f_star * haircut
    f_adj = max(0.0, min(float(max_f), f_adj))
    return {
        "f_star": round(f_star, 6),
        "f_adj": round(f_adj, 6),
        "edge": round(p - c, 6),
        "haircut": round(haircut, 6),
        "fraction": float(fraction),
        "p_uncertainty": round(unc, 6),
    }


def convergence_edge(
    *,
    model_p_up: Optional[float],
    poly_mid: Optional[float],
    ttc_s: float,
    window_seconds: float,
) -> Optional[dict]:
    """Model–market gap weighted by remaining life fraction.

    Late windows: market should converge; persistent gap = exploitable (or toxic).
    """
    if model_p_up is None or poly_mid is None:
        return None
    gap = float(model_p_up) - float(poly_mid)
    ws = max(float(window_seconds), 1.0)
    life = _clamp01(float(ttc_s) / ws)
    # weight peaks mid-late (life ~0.15–0.45) when convergence is informative
    if life > 0.6:
        w = 0.55
    elif life > 0.25:
        w = 1.0
    elif life > 0.08:
        w = 0.85
    else:
        w = 0.4  # last seconds: nowcast dominates
    return {
        "gap": round(gap, 6),
        "abs_gap": round(abs(gap), 6),
        "life_frac": round(life, 4),
        "weighted_edge": round(abs(gap) * w, 6),
        "weight": round(w, 4),
        "model_favors": ("up" if gap > 0.01 else ("down" if gap < -0.01 else "flat")),
    }


def compute_binary_snapshot(
    *,
    s_now: Optional[float],
    s_open: Optional[float],
    sigma_per_sec: Optional[float],
    ttc_s: float,
    window_seconds: float,
    poly_mid: Optional[float],
    model_p_up: Optional[float],
    proposed_side: Optional[str] = None,
    ask: Optional[float] = None,
    rsi_lean: Optional[str] = None,
    rsi_strength: float = 0.75,
    p_uncertainty: float = 0.0,
    kelly_fraction: float = 0.25,
) -> dict:
    """Full pre-trade binary math pack for one window."""
    z = None
    d2 = None
    theta = None
    if (s_now is not None and s_open is not None and sigma_per_sec is not None):
        z = displacement_z(s_now, s_open, sigma_per_sec, ttc_s)
        d2 = binary_d2(s_now, s_open, sigma_per_sec, ttc_s)
        theta = binary_theta(s_now, s_open, sigma_per_sec, ttc_s)

    unc = market_uncertainty(poly_mid)
    ig = information_gain_rsi(
        prior_p_up=model_p_up if model_p_up is not None else poly_mid,
        lean=rsi_lean,
        proposed_side=proposed_side,
        strength=rsi_strength,
    )
    conv = convergence_edge(
        model_p_up=model_p_up,
        poly_mid=poly_mid,
        ttc_s=ttc_s,
        window_seconds=window_seconds,
    )

    kelly = None
    if proposed_side and ask is not None and model_p_up is not None:
        p_win = float(model_p_up) if proposed_side == "up" else (1.0 - float(model_p_up))
        kelly = estimation_error_kelly(
            p_win=p_win, price=float(ask),
            p_uncertainty=p_uncertainty, fraction=kelly_fraction)

    # Composite intelligence score ∈ [0,1] — higher = cleaner binary setup
    parts = []
    if unc is not None:
        # prefer some uncertainty (ATM has edge discovery) but not total chaos
        parts.append(("entropy_fit", 1.0 - abs(unc["entropy_bits"] - 0.85), 0.15))
    if conv is not None:
        parts.append(("convergence", _clamp01(conv["weighted_edge"] / 0.08), 0.25))
    if ig is not None and ig.get("aligned") is True:
        parts.append(("rsi_ig", _clamp01(ig["info_gain_bits"] / 0.15), 0.25))
    elif ig is not None and ig.get("aligned") is False:
        parts.append(("rsi_ig", 0.2, 0.25))  # opposed = weak score
    if z is not None and proposed_side in ("up", "down"):
        # side agrees with displacement
        z_sign = 1.0 if z > 0 else (-1.0 if z < 0 else 0.0)
        side_sign = 1.0 if proposed_side == "up" else -1.0
        align = 1.0 if z_sign * side_sign > 0 else (0.35 if z_sign == 0 else 0.15)
        parts.append(("z_align", align * _clamp01(abs(z) / 1.5), 0.20))
    if kelly is not None:
        parts.append(("kelly_edge", _clamp01(max(0.0, kelly["edge"]) / 0.08), 0.15))

    if parts:
        wsum = sum(w for _, _, w in parts)
        score = sum(s * w for _, s, w in parts) / max(wsum, 1e-9)
    else:
        score = 0.5

    return {
        "enabled": True,
        "observe_only": True,
        "displacement_z": (round(z, 6) if z is not None else None),
        "d2": (round(d2, 6) if d2 is not None else None),
        "theta_per_sec": (round(theta, 10) if theta is not None else None),
        "market_uncertainty": unc,
        "rsi_information_gain": ig,
        "convergence": conv,
        "kelly": kelly,
        "intelligence_score": round(_clamp01(score), 4),
        "components": {name: round(float(s), 4) for name, s, _ in parts},
        "formulas": {
            "z": "ln(S_now/S_open)/(σ√t)",
            "P_up": "Φ(d₂), d₂=[ln(S/K)+(μ-½σ²)t]/(σ√t)",
            "theta": "φ(d₂)·∂d₂/∂t",
            "H": "-p log₂ p -(1-p)log₂(1-p)",
            "IG": "H(prior)-H(posterior), posterior odds = prior odds × LR_rsi",
            "kelly": "f=(p-c)/(1-c) × fraction × (1-2σ_p)",
        },
    }
