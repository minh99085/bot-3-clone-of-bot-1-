"""Continual Bayesian Probability Fusion (CBPF).

Algorithm
---------
For each signal component i with forecast p_i:
  Maintain Dirichlet / Beta reliability α_i, β_i updated online:
    if resolved_yes: α_i += p_i; β_i += (1-p_i)   # soft credit
    else:            α_i += (1-p_i); β_i += p_i

Fusion weights:
  w_i ∝ (α_i / (α_i+β_i)) * exp(-λ * brier_i)

p_swarm = Σ w_i p_i / Σ w_i
p_combined = swarm_weight * p_swarm + market_blend * p_market

Every N=25 resolved trades: re-fit linear fusion via ridge OLS on recent
(component matrix → outcome), optimizing multi-objective:
  L = Brier + log_loss + |calibration_slope - 1|

NEVER touches min_edge / min_conviction.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from hermes.state_io import ensure_dirs, paper_dir

logger = logging.getLogger(__name__)

REFIT_EVERY = 25
HISTORY_MAX = 500


def _path() -> Path:
    return paper_dir() / "cbpf_state.json"


class ContinualBayesianFusion:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _path()
        self.alpha: dict[str, float] = {}
        self.beta: dict[str, float] = {}
        self.brier_sum: dict[str, float] = {}
        self.brier_n: dict[str, int] = {}
        self.history: list[dict[str, Any]] = []
        self.linear_weights: dict[str, float] = {}
        self.swarm_weight: float = 0.70
        self.market_blend: float = 0.30
        self.n_updates: int = 0
        self.last_metrics: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text())
            self.alpha = {k: float(v) for k, v in (raw.get("alpha") or {}).items()}
            self.beta = {k: float(v) for k, v in (raw.get("beta") or {}).items()}
            self.brier_sum = {k: float(v) for k, v in (raw.get("brier_sum") or {}).items()}
            self.brier_n = {k: int(v) for k, v in (raw.get("brier_n") or {}).items()}
            self.history = list(raw.get("history") or [])[-HISTORY_MAX:]
            self.linear_weights = {
                k: float(v) for k, v in (raw.get("linear_weights") or {}).items()
            }
            self.swarm_weight = float(raw.get("swarm_weight", 0.70))
            self.market_blend = float(raw.get("market_blend", 0.30))
            self.n_updates = int(raw.get("n_updates", 0))
            self.last_metrics = dict(raw.get("last_metrics") or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("cbpf load failed: %s", exc)

    def save(self) -> None:
        ensure_dirs()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "alpha": self.alpha,
            "beta": self.beta,
            "brier_sum": self.brier_sum,
            "brier_n": self.brier_n,
            "history": self.history[-HISTORY_MAX:],
            "linear_weights": self.linear_weights,
            "swarm_weight": self.swarm_weight,
            "market_blend": self.market_blend,
            "n_updates": self.n_updates,
            "last_metrics": self.last_metrics,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)

    def update(
        self,
        components: dict[str, float],
        *,
        resolved_yes: bool,
        p_market: float,
    ) -> None:
        y = 1.0 if resolved_yes else 0.0
        for name, p in components.items():
            p = float(max(1e-6, min(1.0 - 1e-6, p)))
            a = self.alpha.setdefault(name, 1.0)
            b = self.beta.setdefault(name, 1.0)
            if resolved_yes:
                self.alpha[name] = a + p
                self.beta[name] = b + (1.0 - p)
            else:
                self.alpha[name] = a + (1.0 - p)
                self.beta[name] = b + p
            err = (p - y) ** 2
            self.brier_sum[name] = float(self.brier_sum.get(name, 0.0)) + err
            self.brier_n[name] = int(self.brier_n.get(name, 0)) + 1

        self.history.append(
            {
                "components": {k: float(v) for k, v in components.items()},
                "y": y,
                "p_market": float(p_market),
            }
        )
        self.history = self.history[-HISTORY_MAX:]
        self.n_updates += 1
        if self.n_updates % REFIT_EVERY == 0:
            self.refit()
        self.save()

    def reliability(self, name: str) -> float:
        a = self.alpha.get(name, 1.0)
        b = self.beta.get(name, 1.0)
        base = a / (a + b)
        n = self.brier_n.get(name, 0)
        if n <= 0:
            return base
        brier = self.brier_sum.get(name, 0.0) / n
        return float(base * math.exp(-4.0 * brier))

    def fuse(
        self,
        components: dict[str, float],
        *,
        p_market: float,
    ) -> float:
        if self.linear_weights:
            # Prefer ridge-refit weights when available
            num = 0.0
            den = 0.0
            for name, p in components.items():
                w = float(self.linear_weights.get(name, 0.0))
                if w <= 0:
                    w = self.reliability(name)
                num += w * float(p)
                den += w
            p_swarm = num / den if den > 0 else 0.5
        else:
            num = den = 0.0
            for name, p in components.items():
                w = self.reliability(name)
                num += w * float(p)
                den += w
            p_swarm = num / den if den > 0 else 0.5
        sw = self.swarm_weight
        mb = self.market_blend
        tot = sw + mb
        if tot <= 0:
            return float(max(0.05, min(0.95, p_swarm)))
        return float(
            max(0.05, min(0.95, (sw / tot) * p_swarm + (mb / tot) * float(p_market)))
        )

    def refit(self) -> dict[str, float]:
        """Ridge OLS + multi-objective score on recent history."""
        if len(self.history) < 15:
            return {}
        names = sorted({k for h in self.history for k in h["components"]})
        if not names:
            return {}
        X = []
        y = []
        for h in self.history:
            X.append([float(h["components"].get(n, 0.5)) for n in names])
            y.append(float(h["y"]))
        X_arr = np.asarray(X, dtype=float)
        y_arr = np.asarray(y, dtype=float)
        # Ridge
        lam = 1e-2
        xtx = X_arr.T @ X_arr + lam * np.eye(X_arr.shape[1])
        try:
            w = np.linalg.solve(xtx, X_arr.T @ y_arr)
        except np.linalg.LinAlgError:
            w = np.linalg.lstsq(xtx, X_arr.T @ y_arr, rcond=None)[0]
        w = np.maximum(w, 0.0)
        if w.sum() <= 0:
            w = np.ones_like(w)
        w = w / w.sum()
        pred = X_arr @ w
        pred = np.clip(pred, 1e-6, 1 - 1e-6)
        brier = float(np.mean((pred - y_arr) ** 2))
        logloss = float(
            -np.mean(y_arr * np.log(pred) + (1 - y_arr) * np.log(1 - pred))
        )
        # Calibration slope via OLS y ~ a + b pred
        var_p = float(np.var(pred))
        if var_p > 1e-12:
            cov = float(np.mean((pred - pred.mean()) * (y_arr - y_arr.mean())))
            slope = cov / var_p
        else:
            slope = 1.0
        cal_pen = abs(slope - 1.0)
        loss = brier + logloss + cal_pen
        self.linear_weights = {n: float(wi) for n, wi in zip(names, w)}
        # Soft-adjust swarm/market blend from Brier (same spirit as signal_calibration)
        swarm = float(max(0.55, min(0.80, 0.80 - 1.0 * max(0.0, min(0.25, brier - 0.10)))))
        self.swarm_weight = swarm
        self.market_blend = round(1.0 - swarm, 4)
        self.last_metrics = {
            "brier": brier,
            "logloss": logloss,
            "calibration_slope": float(slope),
            "multi_obj": float(loss),
            "n": float(len(y)),
        }
        logger.info(
            "cbpf refit: brier=%.4f logloss=%.4f slope=%.3f swarm=%.2f",
            brier,
            logloss,
            slope,
            swarm,
        )
        self.save()
        return self.last_metrics

    def mutable_export(self) -> dict[str, Any]:
        return {
            "swarm_weight": self.swarm_weight,
            "market_blend": self.market_blend,
            "cbpf_dirichlet_strength": float(
                np.mean(list(self.alpha.values())) if self.alpha else 1.0
            ),
        }


_CBPF: Optional[ContinualBayesianFusion] = None


def get_cbpf() -> ContinualBayesianFusion:
    global _CBPF
    if _CBPF is None:
        _CBPF = ContinualBayesianFusion()
    return _CBPF
