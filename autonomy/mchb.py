"""Meta-Contextual Hierarchical Bandit (MCHB).

Algorithm
---------
Level-1 (family): Thompson Sampling over Beta(α,β) per strategy family.
Level-2 (leaf):   LinUCB over context features within the chosen family.

  x_t = [vol_onehot, ttr_onehot, liq, sentiment, hour_sin, hour_cos, |disloc|]
  For each arm a ∈ {exploit, explore, skip}:
      θ̂_a = A_a^{-1} b_a
      UCB_a = θ̂_a·x + α √(xᵀ A_a^{-1} x)
  Pick arm with max UCB among level-1 survivors.

Explore only when predictive uncertainty √(xᵀ A^{-1} x) > τ
(else force exploit/skip by family posterior).

Reward = SettlementReward.as_unit_reward()  (risk-adj PnL + Brier).

State: data/paper/<instance>/mchb_state.json
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from autonomy.schemas import ContextFeatures, Sentiment, TTRBucket, VolRegime
from hermes.state_io import ensure_dirs, paper_dir

logger = logging.getLogger(__name__)

ARMS = ("exploit", "explore", "skip")
FEATURE_DIM = 12
DEFAULT_ALPHA = 0.6
UNCERTAINTY_TAU = 0.35


def _state_path() -> Path:
    return paper_dir() / "mchb_state.json"


def context_to_features(ctx: ContextFeatures) -> np.ndarray:
    """Dense feature vector for LinUCB."""
    vol = [0.0, 0.0, 0.0]
    vol[{"low": 0, "mid": 1, "high": 2}[ctx.vol_regime.value]] = 1.0
    ttr = [0.0, 0.0, 0.0]
    ttr[{"early": 0, "mid": 1, "late": 2}[ctx.ttr_bucket.value]] = 1.0
    sent = {"bear": -1.0, "neutral": 0.0, "bull": 1.0}[ctx.sentiment.value]
    h = float(ctx.hour)
    hour_sin = math.sin(2 * math.pi * h / 24.0)
    hour_cos = math.cos(2 * math.pi * h / 24.0)
    vec = np.asarray(
        [
            *vol,
            *ttr,
            float(ctx.liq_score),
            sent,
            hour_sin,
            hour_cos,
            min(1.0, abs(float(ctx.dislocation)) / 0.15),
            float(ctx.hurst if ctx.hurst is not None else 0.5),
        ],
        dtype=float,
    )
    assert vec.size == FEATURE_DIM
    return vec


@dataclass
class BetaArm:
    alpha: float = 1.0
    beta: float = 1.0
    pulls: int = 0

    def sample(self) -> float:
        return random.betavariate(max(1e-3, self.alpha), max(1e-3, self.beta))

    def update(self, reward: float) -> None:
        r = max(0.0, min(1.0, reward))
        self.alpha += r
        self.beta += 1.0 - r
        self.pulls += 1

    def to_dict(self) -> dict[str, float]:
        return {"alpha": self.alpha, "beta": self.beta, "pulls": self.pulls}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BetaArm":
        return cls(
            alpha=float(d.get("alpha", 1.0)),
            beta=float(d.get("beta", 1.0)),
            pulls=int(d.get("pulls", 0)),
        )


@dataclass
class LinUCBArm:
    A: np.ndarray = field(default_factory=lambda: np.eye(FEATURE_DIM))
    b: np.ndarray = field(default_factory=lambda: np.zeros(FEATURE_DIM))
    pulls: int = 0

    def ucb(self, x: np.ndarray, alpha: float = DEFAULT_ALPHA) -> tuple[float, float]:
        try:
            A_inv = np.linalg.inv(self.A)
        except np.linalg.LinAlgError:
            A_inv = np.linalg.pinv(self.A)
        theta = A_inv @ self.b
        var = float(x @ A_inv @ x)
        unc = math.sqrt(max(var, 0.0))
        return float(theta @ x) + alpha * unc, unc

    def update(self, x: np.ndarray, reward: float) -> None:
        r = max(0.0, min(1.0, reward))
        self.A = self.A + np.outer(x, x)
        self.b = self.b + r * x
        self.pulls += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "A": self.A.tolist(),
            "b": self.b.tolist(),
            "pulls": self.pulls,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LinUCBArm":
        A = np.asarray(d.get("A") or np.eye(FEATURE_DIM), dtype=float)
        b = np.asarray(d.get("b") or np.zeros(FEATURE_DIM), dtype=float)
        if A.shape != (FEATURE_DIM, FEATURE_DIM):
            A = np.eye(FEATURE_DIM)
        if b.shape != (FEATURE_DIM,):
            b = np.zeros(FEATURE_DIM)
        return cls(A=A, b=b, pulls=int(d.get("pulls", 0)))


@dataclass
class MCHBDecision:
    family: str
    arm: str
    context_key: str
    uncertainty: float
    forced_exploit: bool
    scores: dict[str, float]


class MetaContextualBandit:
    """Hierarchical Thompson (family) + LinUCB (leaf arms)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _state_path()
        self.families: dict[str, BetaArm] = {}
        self.leaves: dict[str, dict[str, LinUCBArm]] = {}  # leaf_key → arm → LinUCB
        self.disabled_families: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text())
            for k, v in (raw.get("families") or {}).items():
                self.families[k] = BetaArm.from_dict(v)
            for lk, arms in (raw.get("leaves") or {}).items():
                self.leaves[lk] = {
                    a: LinUCBArm.from_dict(ad) for a, ad in arms.items()
                }
            self.disabled_families = set(raw.get("disabled_families") or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("mchb load failed: %s", exc)

    def save(self) -> None:
        ensure_dirs()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "families": {k: v.to_dict() for k, v in self.families.items()},
            "leaves": {
                lk: {a: arm.to_dict() for a, arm in arms.items()}
                for lk, arms in self.leaves.items()
            },
            "disabled_families": sorted(self.disabled_families),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)

    def decide(self, ctx: ContextFeatures) -> MCHBDecision:
        fam_key = ctx.family_key()
        if fam_key in self.disabled_families:
            return MCHBDecision(
                family=fam_key,
                arm="skip",
                context_key=ctx.leaf_key(),
                uncertainty=0.0,
                forced_exploit=True,
                scores={"skip": 1.0},
            )
        fam = self.families.setdefault(fam_key, BetaArm())
        fam_score = fam.sample()
        # If family posterior mean is terrible, skip
        if fam.pulls >= 8 and fam.alpha / (fam.alpha + fam.beta) < 0.35:
            return MCHBDecision(
                family=fam_key,
                arm="skip",
                context_key=ctx.leaf_key(),
                uncertainty=0.0,
                forced_exploit=True,
                scores={"family": fam_score, "skip": 1.0},
            )

        leaf = ctx.leaf_key()
        arms = self.leaves.setdefault(
            leaf, {a: LinUCBArm() for a in ARMS}
        )
        x = context_to_features(ctx)
        scores: dict[str, float] = {}
        uncs: dict[str, float] = {}
        for a in ARMS:
            u, unc = arms[a].ucb(x)
            scores[a] = u
            uncs[a] = unc
        max_unc = max(uncs.values()) if uncs else 0.0
        # Auto-explore only when uncertainty high
        if max_unc < UNCERTAINTY_TAU:
            # exploit vs skip by family score
            arm = "exploit" if fam_score >= 0.45 else "skip"
            forced = True
        else:
            arm = max(scores, key=scores.get)  # type: ignore[arg-type]
            forced = False
        return MCHBDecision(
            family=fam_key,
            arm=arm,
            context_key=leaf,
            uncertainty=float(max_unc),
            forced_exploit=forced,
            scores={**scores, "family_thompson": fam_score},
        )

    def update(
        self,
        ctx: ContextFeatures,
        arm: str,
        reward: float,
    ) -> None:
        fam_key = ctx.family_key()
        self.families.setdefault(fam_key, BetaArm()).update(reward)
        leaf = ctx.leaf_key()
        arms = self.leaves.setdefault(leaf, {a: LinUCBArm() for a in ARMS})
        if arm not in arms:
            arms[arm] = LinUCBArm()
        arms[arm].update(context_to_features(ctx), reward)
        self.save()

    def disable_family(self, family: str) -> None:
        self.disabled_families.add(family)
        self.save()

    def enable_family(self, family: str) -> None:
        self.disabled_families.discard(family)
        self.save()

    def summary(self) -> dict[str, Any]:
        return {
            "n_families": len(self.families),
            "n_leaves": len(self.leaves),
            "disabled": sorted(self.disabled_families),
            "family_means": {
                k: v.alpha / (v.alpha + v.beta) for k, v in self.families.items()
            },
        }


def build_context_from_meta(
    *,
    timeframe: str = "5m",
    seconds_to_resolution: float = 300.0,
    liquidity_usd: float = 5_000.0,
    momentum: float = 0.0,
    dislocation: float = 0.0,
    hurst: Optional[float] = None,
    hour: Optional[int] = None,
    category: str = "crypto",
    family: str = "mispricing",
    garch_vol: Optional[float] = None,
) -> ContextFeatures:
    """Map live signal meta → ContextFeatures."""
    from datetime import datetime, timezone

    h = hour if hour is not None else datetime.now(timezone.utc).hour
    # TTR
    if seconds_to_resolution > 600:
        ttr = TTRBucket.EARLY
    elif seconds_to_resolution > 180:
        ttr = TTRBucket.MID
    else:
        ttr = TTRBucket.LATE
    # Vol from garch or |momentum|
    gv = float(garch_vol or 0.0)
    if gv > 0.8 or abs(momentum) > 0.7:
        vol = VolRegime.HIGH
    elif gv > 0.3 or abs(momentum) > 0.3:
        vol = VolRegime.MID
    else:
        vol = VolRegime.LOW
    if momentum > 0.25:
        sent = Sentiment.BULL
    elif momentum < -0.25:
        sent = Sentiment.BEAR
    else:
        sent = Sentiment.NEUTRAL
    liq = max(0.0, min(1.0, math.log10(max(liquidity_usd, 10.0)) / 6.0))
    return ContextFeatures(
        vol_regime=vol,
        ttr_bucket=ttr,
        liq_score=liq,
        sentiment=sent,
        category=category,
        timeframe=timeframe,
        family=family,
        hour=int(h),
        dislocation=float(dislocation),
        hurst=hurst,
    )


_MCHB: Optional[MetaContextualBandit] = None


def get_mchb() -> MetaContextualBandit:
    global _MCHB
    if _MCHB is None:
        _MCHB = MetaContextualBandit()
    return _MCHB
