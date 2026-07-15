"""Contextual bandit (Thompson Sampling) for explore vs exploit on BTC up/down.

Arms:
  - exploit  — take sized mispricing trade (higher size)
  - explore  — take smaller probe trade to learn
  - skip     — do not trade this turn

Context is discretized into buckets so we learn which market conditions
work (dislocation strength × timeframe × hour regime).

State persists to data/paper/bandit_state.json for 24/7 continuity.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes.mispricing import MispricingSignal
from hermes.state_io import DATA, ensure_dirs

logger = logging.getLogger(__name__)

ARMS = ("exploit", "explore", "skip")
STATE_PATH = DATA / "paper" / "bandit_state.json"


def _beta_sample(alpha: float, beta: float) -> float:
    # random.betavariate requires >0
    a = max(1e-3, alpha)
    b = max(1e-3, beta)
    return random.betavariate(a, b)


def context_key(mp: MispricingSignal, hour: int) -> str:
    """Discretize context for independent Beta posteriors."""
    strength = "none"
    d = abs(mp.dislocation)
    if d >= 0.10:
        strength = "strong"
    elif d >= 0.06:
        strength = "med"
    elif d >= 0.04:
        strength = "weak"
    mom = "flat"
    if mp.cex_momentum > 0.25:
        mom = "up"
    elif mp.cex_momentum < -0.25:
        mom = "down"
    tod = "asia" if 0 <= hour < 8 else ("eu" if hour < 16 else "us")
    return f"{mp.timeframe}|{strength}|{mom}|{tod}"


@dataclass
class ArmStats:
    alpha: float = 1.0  # wins + prior
    beta: float = 1.0  # losses + prior
    pulls: int = 0
    reward_sum: float = 0.0

    def sample(self) -> float:
        return _beta_sample(self.alpha, self.beta)

    def update(self, reward: float) -> None:
        """reward in [0,1] — win≈1, loss≈0; partial for EV-based."""
        r = max(0.0, min(1.0, reward))
        self.alpha += r
        self.beta += 1.0 - r
        self.pulls += 1
        self.reward_sum += r

    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


@dataclass
class BanditDecision:
    arm: str  # exploit | explore | skip
    context: str
    sampled: dict[str, float] = field(default_factory=dict)
    size_scale: float = 0.0  # multiply cold-start size
    reason: str = ""
    explore_rate_est: float = 0.0

    def as_meta(self) -> dict[str, Any]:
        return {
            "bandit_arm": self.arm,
            "bandit_context": self.context,
            "bandit_size_scale": self.size_scale,
            "bandit_reason": self.reason,
            "bandit_samples": {k: round(v, 4) for k, v in self.sampled.items()},
        }


class ContextualBandit:
    """Thompson Sampling over (context → arms)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or STATE_PATH
        self.arms: dict[str, dict[str, ArmStats]] = {}  # ctx -> arm -> stats
        self.global_pulls = 0
        self.global_explore = 0
        self.global_exploit = 0
        self.global_skip = 0
        self._load()

    def _ensure_ctx(self, ctx: str) -> dict[str, ArmStats]:
        if ctx not in self.arms:
            # Mild prior: skip slightly preferred until evidence; explore/exploit equal
            self.arms[ctx] = {
                "exploit": ArmStats(alpha=1.2, beta=1.2),
                "explore": ArmStats(alpha=1.5, beta=1.0),  # encourage early exploration
                "skip": ArmStats(alpha=1.0, beta=1.0),
            }
        return self.arms[ctx]

    def decide(self, mp: MispricingSignal, hour: int) -> BanditDecision:
        ctx = context_key(mp, hour)
        stats = self._ensure_ctx(ctx)
        samples = {arm: stats[arm].sample() for arm in ARMS}

        # Soft constraint: if no active mispricing, bias toward skip but allow explore
        if not mp.active:
            samples["exploit"] *= 0.15
            samples["explore"] *= 0.55
            samples["skip"] *= 1.25
        else:
            # Boost exploit when conviction high
            samples["exploit"] *= 0.8 + 0.6 * mp.conviction
            samples["explore"] *= 1.0
            samples["skip"] *= max(0.3, 1.0 - mp.conviction)

        # Force minimum exploration early in life
        if self.global_pulls < 12 and mp.active:
            samples["explore"] = max(samples["explore"], samples["exploit"] + 0.05)
            samples["skip"] *= 0.5

        arm = max(samples, key=samples.get)
        size_scale = 0.0
        if arm == "exploit":
            size_scale = 1.0 + 0.8 * mp.conviction  # up to ~1.8x cold start
        elif arm == "explore":
            size_scale = 0.5  # half size probe
        reason = (
            f"TS ctx={ctx} arm={arm} "
            f"samples={{'exploit':{samples['exploit']:.3f},"
            f"'explore':{samples['explore']:.3f},"
            f"'skip':{samples['skip']:.3f}}} "
            f"mp_active={mp.active} conv={mp.conviction:.2f}"
        )
        explore_rate = (
            self.global_explore / max(1, self.global_exploit + self.global_explore)
        )
        return BanditDecision(
            arm=arm,
            context=ctx,
            sampled=samples,
            size_scale=size_scale,
            reason=reason,
            explore_rate_est=explore_rate,
        )

    def record_pull(self, decision: BanditDecision) -> None:
        self.global_pulls += 1
        if decision.arm == "explore":
            self.global_explore += 1
        elif decision.arm == "exploit":
            self.global_exploit += 1
        else:
            self.global_skip += 1
        self._save()

    def update_reward(self, context: str, arm: str, reward: float) -> None:
        stats = self._ensure_ctx(context)
        if arm not in stats:
            stats[arm] = ArmStats()
        stats[arm].update(reward)
        self._save()
        logger.info(
            "bandit update ctx=%s arm=%s reward=%.2f mean=%.3f pulls=%d",
            context,
            arm,
            reward,
            stats[arm].mean(),
            stats[arm].pulls,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "global_pulls": self.global_pulls,
            "global_explore": self.global_explore,
            "global_exploit": self.global_exploit,
            "global_skip": self.global_skip,
            "explore_rate": round(
                self.global_explore / max(1, self.global_exploit + self.global_explore), 3
            ),
            "n_contexts": len(self.arms),
            "top_contexts": sorted(
                (
                    {
                        "ctx": ctx,
                        "exploit_mean": round(arms["exploit"].mean(), 3),
                        "explore_mean": round(arms["explore"].mean(), 3),
                        "skip_mean": round(arms["skip"].mean(), 3),
                        "pulls": sum(a.pulls for a in arms.values()),
                    }
                    for ctx, arms in self.arms.items()
                ),
                key=lambda x: -x["pulls"],
            )[:8],
        }

    def _save(self) -> None:
        ensure_dirs()
        payload = {
            "global_pulls": self.global_pulls,
            "global_explore": self.global_explore,
            "global_exploit": self.global_exploit,
            "global_skip": self.global_skip,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "arms": {
                ctx: {
                    arm: {
                        "alpha": s.alpha,
                        "beta": s.beta,
                        "pulls": s.pulls,
                        "reward_sum": s.reward_sum,
                    }
                    for arm, s in arms.items()
                }
                for ctx, arms in self.arms.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.global_pulls = int(data.get("global_pulls") or 0)
            self.global_explore = int(data.get("global_explore") or 0)
            self.global_exploit = int(data.get("global_exploit") or 0)
            self.global_skip = int(data.get("global_skip") or 0)
            for ctx, arms in (data.get("arms") or {}).items():
                self.arms[ctx] = {}
                for arm, s in arms.items():
                    self.arms[ctx][arm] = ArmStats(
                        alpha=float(s.get("alpha", 1)),
                        beta=float(s.get("beta", 1)),
                        pulls=int(s.get("pulls", 0)),
                        reward_sum=float(s.get("reward_sum", 0)),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("bandit state load failed: %s", exc)


_BANDIT: Optional[ContextualBandit] = None


def get_bandit() -> ContextualBandit:
    global _BANDIT
    if _BANDIT is None:
        _BANDIT = ContextualBandit()
    return _BANDIT
