"""PRISM Phase 5 — Thompson-sampling bucket posteriors (PAPER ONLY).

Replace hard-coded asset@timing whitelists with Beta posteriors per behavioral cell::

    bucket = (asset, minute_band, regime, tv_pattern)   ->   Beta(alpha, beta)

Each settled directional trade updates its bucket (win -> alpha+1, loss -> beta+1). Thompson
sampling draws ``p_win ~ Beta(alpha, beta)`` for exploration; the expected win rate and a Wilson
bound drive probe / sniper-allowed / block decisions. Observe-only learning: recording happens on
every settle, but the *gate* (block_bucket) only restricts on the legacy directional path and is
disabled by default so the live Osmani soak is untouched. PAPER ONLY.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine.pulse.selectivity import _wilson_upper

logger = logging.getLogger("pulse.prism.thompson")

_STORE_FILENAME = "prism_thompson.json"

# Minute-since-open bands (gaps between 20–35m and >50m collapse to "other").
_MINUTE_BANDS = ((0, 5), (5, 10), (10, 15), (15, 20), (35, 40), (40, 45), (45, 50))

# Pessimistic seed prior for the operator-flagged BNB asset (few wins expected).
_BNB_PRIOR = (2.0, 5.0)
_DEFAULT_PRIOR = (1.0, 1.0)


def minute_band_from_seconds(sso: Optional[float]) -> str:
    if sso is None:
        return "other"
    try:
        m = float(sso) / 60.0
    except (TypeError, ValueError):
        return "other"
    for lo, hi in _MINUTE_BANDS:
        if lo <= m < hi:
            return "%d-%dm" % (lo, hi)
    return "other"


def _asset_from_label(label: Optional[str]) -> str:
    s = str(label or "").strip().lower()
    if not s:
        return "btc"
    head = s.split("_")[0]
    if not head or head.endswith("m") or head.endswith("h"):
        return "btc"                      # bare timeframe label (e.g. "15m") -> default BTC
    return head


@dataclass(frozen=True)
class BucketKey:
    asset: str
    minute_band: str
    regime: str
    tv_pattern: str

    def as_str(self) -> str:
        return "|".join((self.asset, self.minute_band, self.regime, self.tv_pattern))

    @classmethod
    def from_str(cls, s: str) -> "BucketKey":
        parts = (s.split("|") + ["", "", "", ""])[:4]
        return cls(parts[0], parts[1], parts[2], parts[3])


@dataclass
class BucketPosterior:
    alpha: float = 1.0
    beta: float = 1.0
    n: int = 0
    wins: int = 0
    pnl_usd: float = 0.0

    @property
    def expected_p_win(self) -> float:
        tot = self.alpha + self.beta
        return (self.alpha / tot) if tot > 0 else 0.5

    def to_dict(self) -> dict:
        return {"alpha": round(self.alpha, 4), "beta": round(self.beta, 4), "n": self.n,
                "wins": self.wins, "pnl_usd": round(self.pnl_usd, 4)}

    @classmethod
    def from_dict(cls, d: dict) -> "BucketPosterior":
        return cls(alpha=float(d.get("alpha", 1.0)), beta=float(d.get("beta", 1.0)),
                   n=int(d.get("n", 0)), wins=int(d.get("wins", 0)),
                   pnl_usd=float(d.get("pnl_usd", 0.0)))


class ThompsonStore:
    """Disk-bound Beta posteriors keyed by :class:`BucketKey`. PAPER ONLY."""

    def __init__(self, data_dir: Optional[Path] = None, *, bnb_block: bool = False,
                 rng: Optional[random.Random] = None):
        self.data_dir = Path(data_dir) if data_dir else None
        self.bnb_block = bool(bnb_block)
        self._rng = rng or random.Random()
        self.buckets: dict[str, BucketPosterior] = {}
        if self.data_dir is not None:
            self.load()

    @property
    def path(self) -> Optional[Path]:
        return (self.data_dir / _STORE_FILENAME) if self.data_dir is not None else None

    # ---- bucketing -------------------------------------------------------------------------- #
    def key_from_trade(self, research: Optional[dict]) -> BucketKey:
        r = research or {}
        asset = _asset_from_label(r.get("series_label") or r.get("market_series"))
        minute_band = minute_band_from_seconds(r.get("seconds_since_open_at_entry"))
        regime = str(r.get("markov_state") or "unknown")
        tv_pattern = str(r.get("prism_tv_pattern") or "none")
        return BucketKey(asset, minute_band, regime, tv_pattern)

    def _prior_for(self, bucket: BucketKey) -> tuple:
        return _BNB_PRIOR if bucket.asset == "bnb" else _DEFAULT_PRIOR

    def get(self, bucket: BucketKey) -> BucketPosterior:
        k = bucket.as_str()
        post = self.buckets.get(k)
        if post is None:
            a, b = self._prior_for(bucket)
            post = BucketPosterior(alpha=a, beta=b)
            self.buckets[k] = post
        return post

    # ---- learning --------------------------------------------------------------------------- #
    def record(self, bucket: BucketKey, won: bool, pnl: float, *, save: bool = True) -> None:
        post = self.get(bucket)
        post.n += 1
        if won:
            post.alpha += 1.0
            post.wins += 1
        else:
            post.beta += 1.0
        post.pnl_usd += float(pnl or 0.0)
        if save:
            self.save()

    # ---- posteriors ------------------------------------------------------------------------- #
    def sample_p_win(self, bucket: BucketKey) -> float:
        post = self.get(bucket)
        return self._rng.betavariate(max(1e-6, post.alpha), max(1e-6, post.beta))

    def expected_p_win(self, bucket: BucketKey) -> float:
        return self.get(bucket).expected_p_win

    def probe_only(self, bucket: BucketKey) -> bool:
        return self.get(bucket).n < 5

    def sniper_allowed(self, bucket: BucketKey, *, breakeven: float = 0.5) -> bool:
        post = self.get(bucket)
        return post.n >= 15 and post.expected_p_win > (breakeven + 0.05)

    def block_bucket(self, bucket: BucketKey, *, breakeven: float = 0.5, z: float = 1.64) -> bool:
        # Operator hard block for BNB (configurable) — the pessimistic prior alone won't block.
        if self.bnb_block and bucket.asset == "bnb":
            return True
        post = self.get(bucket)
        if post.n < 20:
            return False
        return _wilson_upper(post.wins, post.n, z) < breakeven

    def thompson_confidence_factor(self, bucket: BucketKey) -> float:
        """Multiplier in [0.3, 1.0] applied to the ensemble C. Probe buckets get 0.5 (uncertain);
        proven buckets scale with expected win rate."""
        post = self.get(bucket)
        if post.n < 5:
            return 0.5
        return max(0.3, min(1.0, 0.3 + 0.7 * post.expected_p_win))

    def size_multiplier(self, bucket: BucketKey, ask: Optional[float],
                        sample: Optional[float] = None) -> float:
        """Thompson size multiplier: clamp((sample - ask) / (1 - ask), 0, 1)."""
        if ask is None:
            return 0.0
        try:
            a = float(ask)
        except (TypeError, ValueError):
            return 0.0
        if a >= 1.0:
            return 0.0
        s = sample if sample is not None else self.sample_p_win(bucket)
        return max(0.0, min(1.0, (float(s) - a) / (1.0 - a)))

    # ---- persistence ------------------------------------------------------------------------ #
    def load(self) -> None:
        p = self.path
        if p is None or not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — never break startup on a bad file
            logger.warning("prism thompson: could not read %s", p)
            return
        for k, v in (data.get("buckets") or {}).items():
            if isinstance(v, dict):
                self.buckets[str(k)] = BucketPosterior.from_dict(v)

    def save(self) -> None:
        p = self.path
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "schema": "prism_thompson/1.0",
                "buckets": {k: v.to_dict() for k, v in self.buckets.items()},
            }, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001 — persistence must never crash the loop
            logger.warning("prism thompson: could not write %s", p)

    def report(self, *, top_n: int = 8) -> dict:
        ranked = sorted(self.buckets.items(),
                        key=lambda kv: (kv[1].n, kv[1].expected_p_win), reverse=True)[:top_n]
        return {
            "enabled": True,
            "bnb_block": self.bnb_block,
            "n_buckets": len(self.buckets),
            "top_buckets": [{"bucket": k, **v.to_dict(),
                             "expected_p_win": round(v.expected_p_win, 4)}
                            for k, v in ranked],
        }
