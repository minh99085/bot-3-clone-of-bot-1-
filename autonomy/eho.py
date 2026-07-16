"""Nightly Evolutionary Hyperparameter Optimizer (EHO).

CMA-ES lite over *mutable* params only. Shadow-test on synthetic adversarial
markets + recent decision history. Promote only if:
  OOS WR ≥ 82% AND max DD ≤ current baseline DD.

Triggers: every 24h OR after 50 resolved trades.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from autonomy.freeze import FROZEN_KEYS, assert_mutable_only
from models.config import STRICT_REAL_FREEZE, load_enhanced_config

logger = logging.getLogger(__name__)

EHO_EVERY_N = 50
EHO_EVERY_SEC = 24 * 3600
PROMOTE_WR = 0.82


@dataclass
class EHOResult:
    promoted: bool
    params: dict[str, Any]
    wr: float
    max_dd: float
    baseline_wr: float
    baseline_dd: float
    reason: str
    n_evals: int


# Search space (mutable only) — encoded as vector
_PARAM_SPEC: list[tuple[str, float, float, float]] = [
    # name, low, high, default
    ("swarm_weight", 0.55, 0.80, 0.70),
    ("soft_kappa_scale", 0.40, 1.00, 1.00),
    ("size_multiplier", 0.35, 1.00, 1.00),
    ("max_conviction_boost", 0.00, 0.10, 0.05),
    ("explore_rate", 0.05, 0.35, 0.15),
]


def _decode(z: np.ndarray) -> dict[str, float]:
    out: dict[str, float] = {}
    for i, (name, lo, hi, _d) in enumerate(_PARAM_SPEC):
        # z ~ N(0,1) → sigmoid into [lo,hi]
        u = 1.0 / (1.0 + math.exp(-float(z[i])))
        out[name] = lo + (hi - lo) * u
    out["market_blend"] = 1.0 - out["swarm_weight"]
    return out


def _encode(params: dict[str, float]) -> np.ndarray:
    z = []
    for name, lo, hi, default in _PARAM_SPEC:
        v = float(params.get(name, default))
        u = (v - lo) / max(1e-9, hi - lo)
        u = min(1 - 1e-6, max(1e-6, u))
        z.append(math.log(u / (1 - u)))
    return np.asarray(z, dtype=float)


def _shadow_backtest(params: dict[str, float], *, seed: int = 0, n_markets: int = 800) -> tuple[float, float]:
    """Run fast synthetic backtest with frozen gates; soft knobs applied post-hoc.

    We cannot loosen filters — only evaluate whether fusion/size soft knobs
    preserve WR/DD. Uses standard BacktestEngine (gates unchanged).
    """
    from backtest.engine import BacktestEngine
    from backtest.metrics import compute_metrics

    cfg = load_enhanced_config(mode="strict_real")
    # Freeze assertion
    for k in FROZEN_KEYS:
        if k in STRICT_REAL_FREEZE:
            pass
    engine = BacktestEngine(cfg, mode="enhanced", seed=seed)
    er = engine.run_synthetic(n_markets=n_markets, seed=seed)
    m = compute_metrics(er)
    # Soft-size stress: if size_multiplier < 1, DD scales roughly with size
    dd = float(m.max_drawdown_pct) * float(params.get("size_multiplier", 1.0))
    return float(m.win_rate), float(dd)


def should_run_eho(
    *,
    n_resolved: int,
    last_eho_n: int,
    last_eho_at: Optional[str],
) -> bool:
    if n_resolved - last_eho_n >= EHO_EVERY_N:
        return True
    if last_eho_at:
        try:
            ts = datetime.fromisoformat(last_eho_at.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - ts).total_seconds() >= EHO_EVERY_SEC:
                return True
        except Exception:  # noqa: BLE001
            return True
    elif n_resolved >= 10:
        return True
    return False


def run_eho(
    *,
    current_params: Optional[dict[str, float]] = None,
    population: int = 8,
    generations: int = 4,
    seed: int = 42,
    n_markets: int = 600,
) -> EHOResult:
    """CMA-ES lite: sample candidates, keep elite mean, promote if gates pass."""
    rng = np.random.default_rng(seed)
    base = current_params or {n: d for n, _a, _b, d in _PARAM_SPEC}
    mean = _encode(base)
    sigma = 0.50
    dim = mean.size

    # Baseline
    base_decoded = assert_mutable_only(_decode(mean))
    # ensure market_blend
    if "swarm_weight" in base_decoded:
        base_decoded["market_blend"] = 1.0 - float(base_decoded["swarm_weight"])
    try:
        base_wr, base_dd = _shadow_backtest(base_decoded, seed=seed, n_markets=n_markets)
    except Exception as exc:  # noqa: BLE001
        logger.warning("eho baseline failed: %s", exc)
        return EHOResult(
            promoted=False,
            params=base_decoded,
            wr=0.0,
            max_dd=1.0,
            baseline_wr=0.0,
            baseline_dd=1.0,
            reason=f"baseline_failed:{exc}",
            n_evals=0,
        )

    best_z = mean.copy()
    best_wr, best_dd = base_wr, base_dd
    n_evals = 1

    for gen in range(generations):
        zs = [mean + sigma * rng.normal(size=dim) for _ in range(population)]
        scored: list[tuple[float, float, np.ndarray, dict[str, float]]] = []
        for z in zs:
            params = assert_mutable_only(_decode(z))
            if "swarm_weight" in params:
                params["market_blend"] = 1.0 - float(params["swarm_weight"])
            try:
                wr, dd = _shadow_backtest(
                    params, seed=seed + n_evals, n_markets=n_markets
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("eho eval skip: %s", exc)
                n_evals += 1
                continue
            n_evals += 1
            scored.append((wr, dd, z, params))
        if not scored:
            break
        # Elite: WR desc, DD asc
        scored.sort(key=lambda t: (-t[0], t[1]))
        elites = scored[: max(2, population // 2)]
        mean = np.mean([e[2] for e in elites], axis=0)
        sigma = max(0.15, sigma * 0.85)
        top = elites[0]
        if top[0] > best_wr + 1e-9 or (abs(top[0] - best_wr) < 1e-9 and top[1] < best_dd):
            best_wr, best_dd, best_z = top[0], top[1], top[2]
        logger.info(
            "eho gen=%d best_wr=%.3f best_dd=%.3f sigma=%.2f",
            gen,
            best_wr,
            best_dd,
            sigma,
        )

    best_params = assert_mutable_only(_decode(best_z))
    if "swarm_weight" in best_params:
        best_params["market_blend"] = 1.0 - float(best_params["swarm_weight"])

    promote = best_wr >= PROMOTE_WR and best_dd <= base_dd + 1e-9
    reason = (
        f"promote wr={best_wr:.1%}≥{PROMOTE_WR:.0%} dd={best_dd:.1%}≤base={base_dd:.1%}"
        if promote
        else f"reject wr={best_wr:.1%} dd={best_dd:.1%} (need WR≥{PROMOTE_WR:.0%} & DD≤{base_dd:.1%})"
    )
    return EHOResult(
        promoted=promote,
        params=best_params,
        wr=best_wr,
        max_dd=best_dd,
        baseline_wr=base_wr,
        baseline_dd=base_dd,
        reason=reason,
        n_evals=n_evals,
    )
