"""Regime-Aware Self-Supervised Pretrain + Fine-Tune (RASP).

Pure numpy (no torch):
  1. Regime detection via HMM-style 2-state EM on returns (vol clustering)
  2. Contrastive-ish representation: z-score windows; distance to regime centroid
  3. Fine-tune: per-regime directional bias + vol scale used by fusion
  4. Synthetic hard examples: liquidity shocks + late flips

Outputs regime_weights dict consumed by CBPF / advanced fusion.
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


def _path() -> Path:
    return paper_dir() / "rasp_state.json"


def detect_regimes(returns: Sequence[float], *, n_iter: int = 15) -> dict[str, Any]:
    """2-state Gaussian HMM EM on |returns| (low-vol / high-vol)."""
    r = np.asarray(returns, dtype=float)
    if r.size < 20:
        return {
            "active": "mid",
            "probs": [0.5, 0.5],
            "means": [0.0, 0.0],
            "stds": [0.01, 0.02],
        }
    x = np.abs(r)
    # Init
    med = float(np.median(x))
    mu = np.array([med * 0.6, med * 1.8])
    std = np.array([med * 0.4 + 1e-6, med * 1.0 + 1e-6])
    pi = np.array([0.6, 0.4])
    for _ in range(n_iter):
        # E-step
        dens = []
        for k in range(2):
            dens.append(
                (1.0 / (math.sqrt(2 * math.pi) * std[k]))
                * np.exp(-0.5 * ((x - mu[k]) / std[k]) ** 2)
            )
        dens_arr = np.vstack(dens)  # 2 x T
        post = dens_arr * pi[:, None]
        post = post / np.maximum(post.sum(axis=0, keepdims=True), 1e-18)
        # M-step
        nk = post.sum(axis=1)
        pi = nk / nk.sum()
        for k in range(2):
            mu[k] = float(np.sum(post[k] * x) / max(nk[k], 1e-9))
            var = float(np.sum(post[k] * (x - mu[k]) ** 2) / max(nk[k], 1e-9))
            std[k] = math.sqrt(max(var, 1e-12))
    last = post[:, -1]
    active = "high" if last[1] > last[0] else "low"
    if abs(last[1] - last[0]) < 0.15:
        active = "mid"
    return {
        "active": active,
        "probs": [float(last[0]), float(last[1])],
        "means": [float(mu[0]), float(mu[1])],
        "stds": [float(std[0]), float(std[1])],
        "pi": [float(pi[0]), float(pi[1])],
    }


def synthetic_hard_examples(
    prices: Sequence[float],
    *,
    n: int = 20,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Generate liquidity-shock and late-flip synthetic windows."""
    rng = np.random.default_rng(seed)
    p = np.asarray(prices, dtype=float)
    if p.size < 10:
        p = 50_000 * np.cumprod(1 + rng.normal(0, 0.001, size=80))
    out: list[dict[str, Any]] = []
    for i in range(n):
        base = p.copy()
        kind = "liq_shock" if i % 2 == 0 else "late_flip"
        if kind == "liq_shock":
            idx = int(rng.integers(len(base) // 3, len(base) - 2))
            base[idx:] *= 1.0 + float(rng.choice([-1, 1])) * float(rng.uniform(0.004, 0.012))
        else:
            # Trend then flip in last 10%
            flip_at = int(0.9 * len(base))
            direction = float(rng.choice([-1.0, 1.0]))
            for j in range(flip_at, len(base)):
                base[j] = base[j - 1] * (1.0 - direction * 0.002)
        out.append(
            {
                "kind": kind,
                "prices": base.tolist(),
                "up": bool(base[-1] > base[0]),
            }
        )
    return out


def contrastive_centroid(windows: list[np.ndarray]) -> np.ndarray:
    """Mean z-scored window as a cheap self-supervised prototype."""
    if not windows:
        return np.zeros(16)
    zs = []
    for w in windows:
        w = np.asarray(w, dtype=float)
        if w.size < 4:
            continue
        # resample to 16
        idx = np.linspace(0, w.size - 1, 16)
        ww = np.interp(idx, np.arange(w.size), w)
        ww = (ww - ww.mean()) / (ww.std() + 1e-9)
        zs.append(ww)
    if not zs:
        return np.zeros(16)
    return np.mean(np.vstack(zs), axis=0)


class RegimeModelStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _path()
        self.regime_weights: dict[str, dict[str, float]] = {
            "low": {"momentum": 0.20, "ou": 0.30, "multi_tf": 0.15, "obi": 0.15, "kalman": 0.20},
            "mid": {"momentum": 0.25, "ou": 0.15, "multi_tf": 0.25, "obi": 0.15, "kalman": 0.20},
            "high": {"momentum": 0.30, "ou": 0.05, "multi_tf": 0.30, "obi": 0.20, "kalman": 0.15},
        }
        self.active: str = "mid"
        self.centroids: dict[str, list[float]] = {}
        self.last_fit: Optional[str] = None
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text())
            self.regime_weights = raw.get("regime_weights") or self.regime_weights
            self.active = str(raw.get("active") or "mid")
            self.centroids = raw.get("centroids") or {}
            self.last_fit = raw.get("last_fit")
        except Exception as exc:  # noqa: BLE001
            logger.warning("rasp load failed: %s", exc)

    def save(self) -> None:
        ensure_dirs()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "regime_weights": self.regime_weights,
            "active": self.active,
            "centroids": self.centroids,
            "last_fit": self.last_fit,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)

    def fit_from_prices(self, prices: Sequence[float]) -> dict[str, Any]:
        p = np.asarray(prices, dtype=float)
        if p.size < 8:
            return {"active": self.active}
        rets = np.diff(p) / np.maximum(p[:-1], 1e-12)
        det = detect_regimes(rets.tolist())
        self.active = str(det["active"])
        # Build windows for contrastive centroid
        windows = []
        w = 16
        for i in range(0, max(0, len(p) - w), w // 2):
            windows.append(p[i : i + w])
        hard = synthetic_hard_examples(p.tolist(), n=10)
        for h in hard:
            windows.append(np.asarray(h["prices"][-w:], dtype=float))
        cen = contrastive_centroid(windows)
        self.centroids[self.active] = cen.tolist()
        # Fine-tune: nudge active regime toward momentum if high-vol
        rw = dict(self.regime_weights.get(self.active) or {})
        if self.active == "high":
            rw["momentum"] = min(0.40, rw.get("momentum", 0.3) + 0.02)
            rw["ou"] = max(0.02, rw.get("ou", 0.05) - 0.01)
        elif self.active == "low":
            rw["ou"] = min(0.40, rw.get("ou", 0.3) + 0.02)
            rw["momentum"] = max(0.10, rw.get("momentum", 0.2) - 0.01)
        # Renormalize
        s = sum(rw.values()) or 1.0
        self.regime_weights[self.active] = {k: float(v) / s for k, v in rw.items()}
        from datetime import datetime, timezone

        self.last_fit = datetime.now(timezone.utc).isoformat()
        self.save()
        logger.info("rasp fit active=%s weights=%s", self.active, self.regime_weights[self.active])
        return {"active": self.active, "weights": self.regime_weights[self.active], **det}

    def active_weights(self) -> dict[str, float]:
        return dict(self.regime_weights.get(self.active) or {})


_RASP: Optional[RegimeModelStore] = None


def get_rasp() -> RegimeModelStore:
    global _RASP
    if _RASP is None:
        _RASP = RegimeModelStore()
    return _RASP
