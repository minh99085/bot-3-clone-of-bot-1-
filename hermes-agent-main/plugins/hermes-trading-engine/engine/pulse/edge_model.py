"""Lightweight calibrated edge model for the BTC 5-min pulse (OBSERVE-ONLY, scaffolding-first).

Starts simple: a pure-python online logistic regression over Phase 3-6 ENTRY-TIME features,
trained only on clean labeled samples (entry features -> realized Up/Down). Until ``min_samples``
clean labels exist it returns SCAFFOLDING ONLY (no probability, explicit diagnostic). It has NO
trade authority — outputs (p_up, p_down, p_no_trade, model_confidence, calibration_bucket) are
logged/reported only.

Leakage guard: the model is trained on (entry_features, later_outcome) pairs and predicts from
entry_features alone; the outcome label is NEVER a feature. Feature names are a fixed entry-time
allow-list.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional

# entry-time features only (no outcome-derived fields -> no leakage)
FEATURE_NAMES = ("hurst", "autocorr_lag1", "realized_vol_scaled", "zscore",
                 "signal_strength_signed", "edge_quality_score", "orderbook_imbalance")
LABEL_FIELDS = ("outcome_up", "won", "pnl_usd", "s_close")   # forbidden as features (leakage)


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def calibration_bucket(p: Optional[float]) -> str:
    if p is None or math.isnan(p):
        return "na"
    lo = max(0.0, min(0.9, math.floor(p * 10) / 10.0))
    return f"{lo:.1f}-{lo + 0.1:.1f}"


def extract_features(*, features: Optional[dict], signals: Optional[dict],
                     factors: Optional[dict]) -> dict:
    """Build the entry-time numeric feature vector (impute 0.0 for missing). ENTRY-TIME ONLY."""
    f = features or {}
    s = signals or {}
    fac = factors or {}
    rv = f.get("realized_vol")
    sig_dir = s.get("direction")
    strength = s.get("strength") or 0.0
    signed = strength * (1 if sig_dir == "up" else (-1 if sig_dir == "down" else 0))
    vec = {
        "hurst": f.get("hurst"),
        "autocorr_lag1": f.get("autocorr_lag1"),
        "realized_vol_scaled": (rv * 1e4 if rv is not None else None),
        "zscore": f.get("zscore"),
        "signal_strength_signed": signed,
        "edge_quality_score": fac.get("edge_quality_score"),
        "orderbook_imbalance": fac.get("orderbook_imbalance"),
    }
    return {k: (float(v) if v is not None else 0.0) for k, v in vec.items()}


class EdgeModel:
    def __init__(self, *, min_samples: int = 100, lr: float = 0.05, l2: float = 1e-4):
        self.min_samples = int(min_samples)
        self.lr = float(lr)
        self.l2 = float(l2)
        self.w = {f: 0.0 for f in FEATURE_NAMES}
        self.b = 0.0
        self.n_labeled = 0
        self.calib: dict = {}            # bucket -> {n, up}  (cumulative, for the report table)
        self._recent: deque = deque(maxlen=500)   # recent (pred_p, outcome) for CURRENT calibration

    def observe_label(self, vec: dict, outcome_up: bool) -> None:
        """Online logistic SGD update on a clean (entry_features -> realized outcome) pair."""
        if not isinstance(vec, dict):
            return
        z = self.b + sum(self.w[f] * float(vec.get(f, 0.0)) for f in FEATURE_NAMES)
        p = _sigmoid(z)
        y = 1.0 if outcome_up else 0.0
        err = p - y
        for f in FEATURE_NAMES:
            g = err * float(vec.get(f, 0.0)) + self.l2 * self.w[f]
            self.w[f] -= self.lr * g
        self.b -= self.lr * err
        self.n_labeled += 1
        # calibration tracking of the model's own predictions vs realized
        b = calibration_bucket(p)
        c = self.calib.setdefault(b, {"n": 0, "up": 0})
        c["n"] += 1
        c["up"] += int(bool(outcome_up))
        self._recent.append((p, 1.0 if outcome_up else 0.0))

    @property
    def trained(self) -> bool:
        return self.n_labeled >= self.min_samples

    def predict(self, vec: dict) -> dict:
        """Return observe-only outputs. Scaffolding (None) until enough clean labels exist."""
        if not self.trained:
            return {"observe_only": True, "trained": False,
                    "reason": "insufficient_labeled_samples", "n_labeled": self.n_labeled,
                    "p_up": None, "p_down": None, "p_no_trade": None,
                    "model_confidence": None, "calibration_bucket": "na"}
        z = self.b + sum(self.w[f] * float(vec.get(f, 0.0)) for f in FEATURE_NAMES)
        p_up = _sigmoid(z)
        conf = min(1.0, abs(p_up - 0.5) * 2.0)
        return {"observe_only": True, "trained": True, "reason": "ok",
                "n_labeled": self.n_labeled,
                "p_up": round(p_up, 4), "p_down": round(1.0 - p_up, 4),
                "p_no_trade": round(1.0 - conf, 4), "model_confidence": round(conf, 4),
                "calibration_bucket": calibration_bucket(p_up)}

    def decision_p_up(self, vec: dict) -> Optional[float]:
        """The model's calibrated P(up) for the decision blend. Returns None only if it has never
        learned. WHETHER this is trusted enough to influence a trade is gated separately by the
        engine (sample count + calibration). (predict() is the observe-only report view.)"""
        if self.n_labeled <= 0:
            return None
        z = self.b + sum(self.w[f] * float(vec.get(f, 0.0)) for f in FEATURE_NAMES)
        return _sigmoid(z)

    def calibration_error(self, *, min_n: int = 20) -> Optional[float]:
        """Expected calibration error (ECE) over the RECENT prediction window (so it reflects the
        model's CURRENT calibration, not its cold-start history): bin recent predictions into
        deciles and take the sample-weighted mean |mean_confidence - empirical_accuracy|. Lower is
        better. None until enough recent samples. GATES whether the model may influence trades."""
        if len(self._recent) < min_n:
            return None
        bins: dict = {}
        for p, y in self._recent:
            k = min(9, max(0, int(p * 10)))
            agg = bins.setdefault(k, [0.0, 0.0, 0])     # sum_p, sum_y, n
            agg[0] += p
            agg[1] += y
            agg[2] += 1
        total = len(self._recent)
        ece = 0.0
        for agg in bins.values():
            conf = agg[0] / agg[2]
            acc = agg[1] / agg[2]
            ece += (agg[2] / total) * abs(conf - acc)
        return round(ece, 4)

    def calibration_table(self) -> dict:
        return {b: {"n": c["n"], "empirical_up": (round(c["up"] / c["n"], 4) if c["n"] else None)}
                for b, c in sorted(self.calib.items())}

    def report(self, *, affects_trading: bool = False) -> dict:
        return {"enabled": True, "observe_only": (not affects_trading),
                "affects_trading": bool(affects_trading),
                "has_trade_authority": bool(affects_trading), "trained": self.trained,
                "n_labeled": self.n_labeled, "min_samples": self.min_samples,
                "calibration_error": self.calibration_error(),
                "feature_names": list(FEATURE_NAMES),
                "leakage_guard": "entry_features_only; outcome/label never a feature",
                "calibration_table": self.calibration_table()}

    def to_state(self) -> dict:
        return {"w": dict(self.w), "b": self.b, "n_labeled": self.n_labeled,
                "calib": {k: dict(v) for k, v in self.calib.items()},
                "recent": [[round(p, 6), y] for p, y in self._recent]}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        for f in FEATURE_NAMES:
            self.w[f] = float((data.get("w") or {}).get(f, self.w[f]) or 0.0)
        self.b = float(data.get("b", self.b) or 0.0)
        self.n_labeled = int(data.get("n_labeled", 0) or 0)
        self.calib = {k: {"n": int(v.get("n", 0) or 0), "up": int(v.get("up", 0) or 0)}
                      for k, v in (data.get("calib") or {}).items()}
        self._recent = deque(maxlen=500)
        for item in (data.get("recent") or []):
            try:
                self._recent.append((float(item[0]), float(item[1])))
            except Exception:  # noqa: BLE001
                continue
