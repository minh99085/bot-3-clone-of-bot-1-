"""BTC Pulse "Edge Signal" layer (OBSERVE-ONLY): CEX basket momentum + Polymarket stale-price
divergence + time-to-resolution + orderbook pressure, blended into a bounded ``pulse_edge_score``.

Everything here is observe-only: logged per candidate, bucketed in the report, graded vs realized
outcomes for promotion DIAGNOSTICS — but it can NEVER trade, veto, resize, or bypass the execution
gate. The strict execution gate remains the sole trade authority. PAPER ONLY.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------- helpers / buckets --------------------------------------- #
def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def ttc_bucket_edge(ttc_s: Optional[float]) -> str:
    if ttc_s is None:
        return "na"
    if ttc_s >= 240:
        return "240_300s"
    if ttc_s >= 180:
        return "180_240s"
    if ttc_s >= 90:
        return "90_180s"
    if ttc_s >= 30:
        return "30_90s"
    return "0_30s"


def edge_score_bucket(score: Optional[float]) -> str:
    if score is None:
        return "na"
    if score < 0.2:
        return "very_low"
    if score < 0.4:
        return "low"
    if score < 0.6:
        return "medium"
    if score < 0.8:
        return "high"
    return "very_high"


def agreement_bucket(a: Optional[float]) -> str:
    if a is None:
        return "na"
    if a < 0.6:
        return "weak"
    if a < 0.8:
        return "moderate"
    return "strong"


def ob_pressure_bucket(imbalance: Optional[float]) -> str:
    if imbalance is None:
        return "na"
    if imbalance >= 0.2:
        return "bid_heavy"
    if imbalance <= -0.2:
        return "ask_heavy"
    return "balanced"


STALE_CLASSES = ("stale_polymarket_up", "stale_polymarket_down", "already_priced",
                 "not_stale", "insufficient_data")


def classify_stale_divergence(*, cex_return: Optional[float], poly_yes: Optional[float],
                              min_move: float = 0.0005, poly_band: float = 0.04) -> str:
    """Compare CEX-implied pressure (short-horizon basket return) vs the executable Polymarket YES.
    Returns one of STALE_CLASSES (deterministic)."""
    if cex_return is None or poly_yes is None:
        return "insufficient_data"
    if abs(cex_return) < min_move:
        return "not_stale"                       # no CEX pressure to diverge from
    if cex_return > 0:
        return "already_priced" if poly_yes > 0.5 + poly_band else "stale_polymarket_up"
    return "already_priced" if poly_yes < 0.5 - poly_band else "stale_polymarket_down"


# --------------------------------- CEX basket momentum ------------------------------------- #
class CexBasket:
    """Per-exchange timestamped price buffers -> multi-horizon basket returns, velocity,
    acceleration, and cross-exchange agreement. Missing/stale feeds are reported, never fatal."""

    HORIZONS = (15.0, 30.0, 60.0, 180.0)

    def __init__(self, members: list, *, buf: int = 600, stale_s: float = 30.0):
        self.members = list(members or [])
        self.stale_s = float(stale_s)
        self.buf: dict = {m: deque(maxlen=int(buf)) for m in self.members}
        self.missing_reason: dict = {m: "no_data" for m in self.members}

    def observe(self, name: str, price: Optional[float], ts: float,
                *, missing_reason: Optional[str] = None) -> None:
        if name not in self.buf:
            self.buf[name] = deque(maxlen=600)
            self.members.append(name)
        if price is not None and price > 0:
            self.buf[name].append((float(ts), float(price)))
            self.missing_reason[name] = None
        elif missing_reason is not None:
            self.missing_reason.setdefault(name, missing_reason)
            if not self.buf[name]:
                self.missing_reason[name] = missing_reason

    def _ret(self, name: str, now: float, horizon: float) -> Optional[float]:
        dq = self.buf.get(name)
        if not dq:
            return None
        last_ts, last_px = dq[-1]
        if now - last_ts > self.stale_s or last_px <= 0:
            return None
        target = now - horizon
        tol = horizon * 0.5 + 6.0
        best = None
        for ts, px in reversed(dq):
            if ts <= target:
                if abs(target - ts) <= tol and px > 0:
                    best = px
                break
            best_candidate = (ts, px)
        if best is None:
            # fall back to the oldest sample if it is within tolerance of the target
            ts0, px0 = dq[0]
            if last_ts - ts0 >= horizon * 0.5 and px0 > 0:
                best = px0
        if best is None or best <= 0:
            return None
        return (last_px / best) - 1.0

    def coverage(self, now: float) -> dict:
        present, missing = [], {}
        for m in self.members:
            dq = self.buf.get(m)
            if dq and (now - dq[-1][0]) <= self.stale_s:
                present.append(m)
            else:
                missing[m] = (self.missing_reason.get(m)
                              or ("stale" if dq else "no_data"))
        return {"members": list(self.members), "present": present, "missing": missing,
                "n_present": len(present)}

    def momentum(self, now: float) -> dict:
        cov = self.coverage(now)
        present = cov["present"]
        rets = {}
        for h in self.HORIZONS:
            vals = [self._ret(m, now, h) for m in present]
            vals = [v for v in vals if v is not None]
            rets[h] = (sum(vals) / len(vals)) if vals else None
        r15, r30, r60, r180 = (rets[15.0], rets[30.0], rets[60.0], rets[180.0])
        velocity = r30
        acceleration = (2.0 * r15 - r30) if (r15 is not None and r30 is not None) else None
        # cross-exchange agreement on the sign of the 30s return
        signs = []
        for m in present:
            r = self._ret(m, now, 30.0)
            if r is not None and abs(r) > 0:
                signs.append(1 if r > 0 else -1)
        agreement = None
        basket_dir = None
        if signs:
            s = sum(signs)
            basket_dir = "up" if s > 0 else ("down" if s < 0 else "flat")
            if basket_dir in ("up", "down"):
                want = 1 if basket_dir == "up" else -1
                agreement = sum(1 for x in signs if x == want) / len(signs)
        return {"returns": {"r15s": r15, "r30s": r30, "r60s": r60, "r180s": r180},
                "velocity": velocity, "acceleration": acceleration,
                "exchange_agreement": agreement, "basket_direction": basket_dir,
                "coverage": cov}


# --------------------------------- orderbook pressure -------------------------------------- #
def orderbook_pressure(up_book, down_book, *, size_usd: float = 5.0) -> dict:
    """YES/NO spread, depth, VWAP, imbalance (depth pull/add not tracked -> 'na')."""
    out = {"spread": None, "ask_depth_usd": None, "bid_depth_usd": None, "vwap": None,
           "imbalance": None, "depth_pull_add": "na", "bucket": "na"}
    if up_book is None:
        return out
    out["spread"] = up_book.spread
    ask = float(getattr(up_book, "ask_depth_usd", 0.0) or 0.0)
    bid = float(getattr(up_book, "bid_depth_usd", 0.0) or 0.0)
    out["ask_depth_usd"] = ask
    out["bid_depth_usd"] = bid
    if (ask + bid) > 0:
        out["imbalance"] = round((bid - ask) / (bid + ask), 4)
        out["bucket"] = ob_pressure_bucket(out["imbalance"])
    try:
        from engine.pulse.execution_gate import vwap_fill
        vwap, _spent, _sh, _full = vwap_fill(getattr(up_book, "asks", []) or [], size_usd)
        out["vwap"] = (round(vwap, 6) if vwap is not None else None)
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------- pulse_edge_score ---------------------------------------- #
def _regime_support(hurst_regime: Optional[str]) -> Optional[float]:
    if hurst_regime is None:
        return None
    return {"trending": 1.0, "noise": 0.5, "mean_reverting": 0.2}.get(hurst_regime, 0.5)


def compute_pulse_edge_score(*, tv_strength: Optional[float], cex_agreement: Optional[float],
                             stale_class: str, ob_imbalance: Optional[float],
                             basket_direction: Optional[str], hurst_regime: Optional[str],
                             spread: Optional[float], ask_depth_usd: Optional[float],
                             ttc_s: Optional[float], realized_vol: Optional[float],
                             max_spread: float = 0.06, min_depth_usd: float = 50.0) -> dict:
    """Deterministic, bounded [0,1] observe-only edge score = mean(positive components) scaled
    down by mean(penalties). Returns {score, bucket, components, penalties, direction}."""
    pos = {}
    if tv_strength is not None:
        pos["tv_strength"] = _clamp01(tv_strength)
    if cex_agreement is not None:
        pos["cex_agreement"] = _clamp01(cex_agreement)
    if stale_class in ("stale_polymarket_up", "stale_polymarket_down"):
        pos["stale_divergence"] = 1.0
    elif stale_class == "already_priced":
        pos["stale_divergence"] = 0.3
    elif stale_class == "not_stale":
        pos["stale_divergence"] = 0.0
    # orderbook pressure aligned with the basket direction
    if ob_imbalance is not None and basket_direction in ("up", "down"):
        aligned = ob_imbalance if basket_direction == "up" else -ob_imbalance
        pos["ob_pressure_aligned"] = _clamp01((aligned + 1.0) / 2.0)
    rs = _regime_support(hurst_regime)
    if rs is not None:
        pos["regime_support"] = rs

    pen = {}
    if spread is not None:
        pen["spread"] = _clamp01(spread / max_spread)
    if ask_depth_usd is not None:
        pen["depth"] = _clamp01(1.0 - (ask_depth_usd / (min_depth_usd * 4.0)))
    if ttc_s is not None:
        pen["time"] = _clamp01(1.0 - (ttc_s / 300.0))
    if realized_vol is not None:
        pen["volatility"] = _clamp01((realized_vol * 1e4) / 5.0)   # scaled; high vol -> penalty

    base = (sum(pos.values()) / len(pos)) if pos else 0.0
    penalty = (sum(pen.values()) / len(pen)) if pen else 0.0
    score = round(_clamp01(base * (1.0 - 0.5 * penalty)), 4)
    return {"score": score, "bucket": edge_score_bucket(score if pos else None),
            "direction": basket_direction,
            "components": {k: round(v, 4) for k, v in pos.items()},
            "penalties": {k: round(v, 4) for k, v in pen.items()}}


@dataclass
class EdgeSignalSnapshot:
    observe_only: bool = True
    cex_momentum: dict = field(default_factory=dict)
    stale_divergence_class: str = "insufficient_data"
    ttc_bucket: str = "na"
    orderbook_pressure: dict = field(default_factory=dict)
    pulse_edge_score: float = 0.0
    pulse_edge_score_bucket: str = "na"
    cex_agreement_bucket: str = "na"
    direction: Optional[str] = None

    def to_dict(self) -> dict:
        return {"observe_only": True, "cex_momentum": self.cex_momentum,
                "stale_divergence_class": self.stale_divergence_class,
                "ttc_bucket": self.ttc_bucket, "orderbook_pressure": self.orderbook_pressure,
                "pulse_edge_score": self.pulse_edge_score,
                "pulse_edge_score_bucket": self.pulse_edge_score_bucket,
                "cex_agreement_bucket": self.cex_agreement_bucket, "direction": self.direction,
                "affects_trading": False}

    def tags(self) -> dict:
        return {"stale_divergence": self.stale_divergence_class, "ttc_bucket": self.ttc_bucket,
                "ob_pressure": self.orderbook_pressure.get("bucket", "na"),
                "edge_score": self.pulse_edge_score_bucket,
                "cex_agreement": self.cex_agreement_bucket}


# --------------------------------- bucketed learner ---------------------------------------- #
class EdgeSignalLearner:
    """OBSERVE-ONLY bucketed performance + promotion diagnostics for the edge-signal dimensions.
    Mirrors the TradingView learner: win-rate/PnL/EV-after-cost by bucket, best/worst after costs,
    and promotion eligibility (win_rate>=min, EV>0, clean reconciliation, sample size). Never
    promotes on its own; the execution gate stays the sole trade authority."""

    DIMS = ("stale_divergence", "ttc_bucket", "ob_pressure", "edge_score", "cex_agreement")

    def __init__(self):
        self.dims: dict = {d: {} for d in self.DIMS}
        self.settled = 0

    @staticmethod
    def _stat() -> dict:
        return {"n": 0, "wins": 0, "pnl": 0.0, "ev": 0.0, "reconciled_n": 0}

    def record_settled(self, tags: dict, *, won: bool, pnl: float, ev_after_cost: Optional[float],
                       reconciled: bool) -> None:
        self.settled += 1
        won = bool(won)
        pnl = float(pnl or 0.0)
        ev = float(ev_after_cost or 0.0)
        for dim in self.DIMS:
            b = str(tags.get(dim) if tags.get(dim) is not None else "na")
            s = self.dims[dim].setdefault(b, self._stat())
            s["n"] += 1
            s["wins"] += int(won)
            s["pnl"] = round(s["pnl"] + pnl, 6)
            s["ev"] = round(s["ev"] + ev, 6)
            s["reconciled_n"] += int(bool(reconciled))

    @staticmethod
    def _b(s: dict) -> dict:
        n = s["n"]
        return {"n": n, "win_rate": (round(s["wins"] / n, 4) if n else None),
                "pnl_usd": round(s["pnl"], 4),
                "avg_ev_after_cost": (round(s["ev"] / n, 6) if n else None),
                "all_reconciled": (s["reconciled_n"] == n and n > 0)}

    def _ranked(self, *, min_n: int) -> list:
        rows = []
        for dim in self.DIMS:
            for b, s in self.dims[dim].items():
                if b == "na" or s["n"] < min_n:
                    continue
                rows.append({"dimension": dim, "bucket": b, **self._b(s)})
        rows.sort(key=lambda r: (r["avg_ev_after_cost"] if r["avg_ev_after_cost"] is not None
                                 else -9, r["pnl_usd"]), reverse=True)
        return rows

    def promotion_diagnostics(self, *, allowed: bool, min_samples: int,
                              min_win_rate: float = 0.8) -> dict:
        eligible = [r for r in self._ranked(min_n=min_samples)
                    if (r["win_rate"] or 0) >= min_win_rate
                    and (r["avg_ev_after_cost"] or 0) > 0 and r["all_reconciled"]]
        return {"promotion_allowed_by_config": bool(allowed), "min_samples": min_samples,
                "min_win_rate": min_win_rate, "require_positive_ev_after_slippage": True,
                "require_clean_reconciliation": True, "eligible_buckets": eligible,
                "any_eligible": bool(eligible),
                "note": ("observe-only diagnostic; eligible buckets are NOT auto-promoted to "
                         "trading authority unless promotion_allowed_by_config is true AND "
                         "explicitly wired. The execution gate remains the sole trade authority.")}

    def report(self, *, promotion_allowed: bool = False, min_samples: int = 50,
               min_win_rate: float = 0.8, min_rank_n: int = 5) -> dict:
        out = {"observe_only": True, "report_only": True, "affects_trading": False,
               "settled": self.settled}
        for dim in self.DIMS:
            out["by_" + dim] = {b: self._b(s) for b, s in self.dims[dim].items()}
        ranked = self._ranked(min_n=min_rank_n)
        out["best_buckets_after_cost"] = ranked[:3]
        out["worst_buckets_after_cost"] = list(reversed(ranked[-3:])) if ranked else []
        out["promotion"] = self.promotion_diagnostics(
            allowed=promotion_allowed, min_samples=min_samples, min_win_rate=min_win_rate)
        return out

    def to_state(self) -> dict:
        return {"dims": {d: {b: dict(s) for b, s in self.dims[d].items()} for d in self.DIMS},
                "settled": self.settled}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.dims = {d: {} for d in self.DIMS}
        for d in self.DIMS:
            for b, s in (data.get("dims") or {}).get(d, {}).items():
                self.dims[d][b] = {"n": int(s.get("n", 0) or 0), "wins": int(s.get("wins", 0) or 0),
                                   "pnl": float(s.get("pnl", 0.0) or 0.0),
                                   "ev": float(s.get("ev", 0.0) or 0.0),
                                   "reconciled_n": int(s.get("reconciled_n", 0) or 0)}
        self.settled = int(data.get("settled", 0) or 0)


# --------------------------------- engine-facing orchestrator ------------------------------ #
class EdgeSignalEngine:
    """Holds the CEX basket + learner; builds an observe-only EdgeSignalSnapshot per candidate."""

    def __init__(self, members: list, *, stale_s: float = 30.0):
        self.basket = CexBasket(members, stale_s=stale_s)
        self.learner = EdgeSignalLearner()
        self.snapshots = 0

    def observe_prices(self, prices: dict, now: float) -> None:
        """prices: {member_name: (price_or_None, missing_reason_or_None)}."""
        for name, (px, reason) in (prices or {}).items():
            self.basket.observe(name, px, now, missing_reason=reason)

    def snapshot(self, *, now: float, poly_yes: Optional[float], spread: Optional[float],
                 up_book, down_book, ttc_s: Optional[float], hurst_regime: Optional[str],
                 realized_vol: Optional[float], tv_strength: Optional[float],
                 size_usd: float = 5.0) -> EdgeSignalSnapshot:
        mom = self.basket.momentum(now)
        r30 = (mom.get("returns") or {}).get("r30s")
        stale = classify_stale_divergence(cex_return=r30, poly_yes=poly_yes)
        obp = orderbook_pressure(up_book, down_book, size_usd=size_usd)
        agreement = mom.get("exchange_agreement")
        score = compute_pulse_edge_score(
            tv_strength=tv_strength, cex_agreement=agreement, stale_class=stale,
            ob_imbalance=obp.get("imbalance"), basket_direction=mom.get("basket_direction"),
            hurst_regime=hurst_regime, spread=spread,
            ask_depth_usd=obp.get("ask_depth_usd"), ttc_s=ttc_s, realized_vol=realized_vol)
        self.snapshots += 1
        return EdgeSignalSnapshot(
            cex_momentum=mom, stale_divergence_class=stale, ttc_bucket=ttc_bucket_edge(ttc_s),
            orderbook_pressure=obp, pulse_edge_score=score["score"],
            pulse_edge_score_bucket=score["bucket"], cex_agreement_bucket=agreement_bucket(agreement),
            direction=mom.get("basket_direction"))

    def record_settled(self, tags: dict, *, won: bool, pnl: float, ev_after_cost: Optional[float],
                       reconciled: bool) -> None:
        self.learner.record_settled(tags, won=won, pnl=pnl, ev_after_cost=ev_after_cost,
                                    reconciled=reconciled)

    def report(self, *, now: float, promotion_allowed: bool = False, min_samples: int = 50,
               min_win_rate: float = 0.8) -> dict:
        rep = self.learner.report(promotion_allowed=promotion_allowed, min_samples=min_samples,
                                  min_win_rate=min_win_rate)
        rep["observe_only"] = True
        rep["affects_trading"] = False
        rep["snapshots"] = self.snapshots
        rep["cex_basket_coverage"] = self.basket.coverage(now)
        return rep

    def to_state(self) -> dict:
        return {"learner": self.learner.to_state(), "snapshots": self.snapshots}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.learner.load_state(data.get("learner") or {})
        self.snapshots = int(data.get("snapshots", 0) or 0)
