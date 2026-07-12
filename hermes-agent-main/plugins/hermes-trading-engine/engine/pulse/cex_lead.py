"""CEX-lead latency edge for the BTC 5-min pulse (PAPER ONLY).

Hypothesis: CEX spot (Binance/Kraken/Bitstamp/Coinbase) re-prices a few seconds BEFORE the
Polymarket order book does, so the CEX-implied digital probability ``P_cex(up)`` can be a fresher,
more accurate read on the window outcome than the executable Polymarket price ``poly_yes``.

This module makes that hypothesis *falsifiable*. For every window it captures the signal
``divergence = P_cex(up) - poly_yes`` and, at the window close, GRADES it against the realized
outcome — measuring, per divergence-strength bucket:

  * directional accuracy of the signal (does sign(divergence) predict the outcome?),
  * Brier(P_cex) vs Brier(market poly_yes)  -> does CEX BEAT the market, not just a coin flip?
  * hypothetical PnL of taking the signalled side at the market price (after a unit stake), and
  * the Wilson lower bound of the win-rate vs the bucket's own break-even (= avg price paid).

A bucket is only "proven" — and therefore allowed to DRIVE a paper entry (in ``gated`` mode) —
when it clears all of: enough samples, Wilson-lower(win-rate) above break-even, lower Brier than
the market, and positive hypothetical EV. Even then it only proposes a side/probability; the
deterministic safety floor (selectivity, calibration, EV gate, caps, breaker) remains the sole
trade authority. Default mode is ``shadow`` (grade only) so it can never trade until it has earned
it. The 5-min BTC market price is already accurate (~0.21 Brier), so this bar is intentionally high.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional

from engine.pulse.edge_signal import ttc_bucket_edge


def _wilson_lower(correct: int, n: int, z: float = 1.64) -> Optional[float]:
    """One-sided lower bound of the Wilson score interval for a binomial proportion."""
    if n <= 0:
        return None
    phat = correct / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4 * n)) / n)) / denom
    return max(0.0, center - margin)


# divergence-strength buckets on |P_cex(up) - poly_yes|
def div_bucket(abs_div: Optional[float], *, min_divergence: float) -> str:
    if abs_div is None:
        return "na"
    if abs_div < min_divergence:
        return "no_signal"
    if abs_div < 0.08:
        return "0.04-0.08"
    if abs_div < 0.15:
        return "0.08-0.15"
    if abs_div < 0.30:
        return "0.15-0.30"
    return ">=0.30"


class CexLeadEdge:
    """Grades the CEX-lead signal per divergence bucket and (only when proven) proposes a driven
    paper entry. Observe-only by default (``mode='shadow'``)."""

    MODES = ("shadow", "gated")

    def __init__(self, *, enabled: bool = True, mode: str = "shadow", min_samples: int = 60,
                 min_divergence: float = 0.04, confidence_z: float = 1.64,
                 min_edge_vs_market: float = 0.0, ev_margin: float = 0.0,
                 agreement_thr: float = 0.66, tv_strength_thr: float = 0.5,
                 decisive_thr: float = 0.35, late_ttc_s: float = 90.0,
                 kelly_scale: float = 0.5, max_size_frac: float = 2.0):
        self.enabled = bool(enabled)
        self.mode = mode if mode in self.MODES else "shadow"
        self.min_samples = int(min_samples)
        self.min_divergence = float(min_divergence)
        self.confidence_z = float(confidence_z)
        # require CEX Brier to be at least this much BELOW market Brier to count as a real edge
        self.min_edge_vs_market = float(min_edge_vs_market)
        self.ev_margin = float(ev_margin)
        self.agreement_thr = float(agreement_thr)   # cross-exchange agreement to call a signal "confirmed"
        self.tv_strength_thr = float(tv_strength_thr)   # TradingView strength to count as TV-confirmed
        self.decisive_thr = float(decisive_thr)     # |cex_p_up-0.5| >= this => move ~decided (nowcast)
        self.late_ttc_s = float(late_ttc_s)         # ttc <= this => late window (convergence-lag zone)
        self.kelly_scale = float(kelly_scale)       # fraction of full Kelly for edge-scaled sizing
        self.max_size_frac = float(max_size_frac)   # hard cap on size multiplier
        self.buckets: dict = {}                 # context_key -> stat (div + composite microstructure)
        self.graded = 0
        self.signals_seen = 0                   # windows with an actionable signal
        self.drove = 0                          # times it actually proposed a driven entry
        self._recent: deque = deque(maxlen=60)

    def size_fraction(self, *, p_side: float, price: float) -> float:
        """Edge-scaled (fractional-Kelly) size for a PROVEN edge. For a binary bought at ``price``
        with win prob ``p_side``: full Kelly = (p-price)/(1-price); we take ``kelly_scale`` of it,
        clamped to [0, max_size_frac]. Returns 0 when there's no positive edge."""
        try:
            price = float(price)
            if price <= 0 or price >= 1:
                return 0.0
            kelly = (float(p_side) - price) / (1.0 - price)
            return max(0.0, min(self.max_size_frac, kelly * self.kelly_scale))
        except Exception:  # noqa: BLE001
            return 0.0

    # ---------------------------------- signal --------------------------------------------- #
    def signal(self, *, cex_p_up: Optional[float], poly_yes: Optional[float],
               fair: Optional[float] = None, ttc_s: Optional[float] = None,
               basket_direction: Optional[str] = None, exchange_agreement: Optional[float] = None,
               ob_imbalance: Optional[float] = None, tv_direction: Optional[str] = None,
               tv_strength: Optional[float] = None, news_sentiment: Optional[str] = None) -> dict:
        """Build the (observe-only) mispricing signal + ORDERFLOW + TradingView + late-window context.

        Base = CEX-implied vs market divergence. Confirmations: short-horizon CEX move
        (``basket_direction``), cross-exchange agreement, orderbook pressure, and TradingView
        (``tv_direction``/``tv_strength``). LATE-WINDOW NOWCAST: when ttc is low and the CEX nowcast is
        DECISIVE (|cex_p_up-0.5|>=decisive_thr) while the market lags, the outcome is ~decided ->
        the strongest, lowest-variance mispricing. Emits composite ``context_keys`` so grading finds
        which stack (confirmed × TV × late-decisive) actually beats the market."""
        if cex_p_up is None or poly_yes is None:
            return {"has_signal": False, "reason": "missing_inputs", "context_keys": [],
                    "cex_p_up": cex_p_up, "poly_yes": poly_yes}
        div = float(cex_p_up) - float(poly_yes)
        ab = abs(div)
        b = div_bucket(ab, min_divergence=self.min_divergence)
        side = "up" if div > 0 else "down"
        has = ab >= self.min_divergence
        # microstructure confirmation: does the fresh CEX move + breadth + book pressure back the side?
        mom_confirms = (basket_direction == side) if basket_direction in ("up", "down") else None
        agree_strong = (exchange_agreement is not None and float(exchange_agreement) >= self.agreement_thr)
        ob_confirms = (ob_imbalance is not None and ((side == "up" and float(ob_imbalance) > 0)
                                                     or (side == "down" and float(ob_imbalance) < 0)))
        confirmed = bool(mom_confirms and agree_strong)
        # TradingView confirmation: an aligned, strong TV signal on the same side
        tv_dir = (str(tv_direction).lower() if tv_direction else None)
        tv_dir = {"up": "up", "down": "down"}.get(tv_dir)
        tv_confirms = bool(tv_dir == side and tv_strength is not None
                           and float(tv_strength) >= self.tv_strength_thr)
        # late-window nowcast: outcome ~decided by the fresh CEX price while the market lags
        late = (ttc_s is not None and float(ttc_s) <= self.late_ttc_s)
        decisive = abs(float(cex_p_up) - 0.5) >= self.decisive_thr
        late_decisive = bool(late and decisive)
        # Grok news/X sentiment confirmation (Grok exploiting mispricing via fresh context)
        _ns = (str(news_sentiment).lower() if news_sentiment else None)
        news_dir = {"bullish": "up", "bearish": "down"}.get(_ns)
        news_state = ("aligned" if news_dir == side else
                      ("against" if news_dir in ("up", "down") else "neutral"))
        ttcb = ttc_bucket_edge(ttc_s)
        keys = []
        if has:
            self.signals_seen += 1
            keys = [b,                                                  # divergence alone (back-compat)
                    "conf=%s|%s" % (b, "confirmed" if confirmed else "unconfirmed"),
                    "ttc=%s|%s" % (b, ttcb),
                    "tv=%s|%s" % (b, "confirmed" if tv_confirms else "unconfirmed"),
                    "news=%s|%s" % (b, news_state),
                    "late=%s|%s" % (b, "decisive" if late_decisive else "indecisive")]
            if confirmed:
                keys.append("conf_ttc=%s|%s" % (b, ttcb))
            if late_decisive:
                keys.append("latedec=%s" % b)                          # flagship late-window nowcast
            if confirmed and tv_confirms and late_decisive:
                keys.append("stack=%s|aligned" % b)                    # full multi-source alignment
        return {"has_signal": has, "side": side, "divergence": round(div, 4),
                "abs_divergence": round(ab, 4), "bucket": b, "ttc_bucket": ttcb,
                "confirmed": confirmed, "momentum_confirms": mom_confirms,
                "ob_confirms": (bool(ob_confirms) if ob_imbalance is not None else None),
                "tv_confirms": tv_confirms, "tv_direction": tv_dir,
                "news_state": news_state, "news_sentiment": _ns,
                "late_decisive": late_decisive, "decisive": decisive, "late": late,
                "exchange_agreement": (round(float(exchange_agreement), 4)
                                       if exchange_agreement is not None else None),
                "basket_direction": basket_direction, "context_keys": keys,
                "cex_p_up": round(float(cex_p_up), 4), "poly_yes": round(float(poly_yes), 4),
                "fair": (round(float(fair), 4) if fair is not None else None),
                "vs_fair": (round(float(cex_p_up) - float(fair), 4) if fair is not None else None)}

    # ---------------------------------- grading -------------------------------------------- #
    @staticmethod
    def _stat() -> dict:
        return {"n": 0, "correct": 0, "brier_cex": 0.0, "brier_mkt": 0.0, "brier_fair": 0.0,
                "pnl": 0.0, "breakeven_sum": 0.0, "wins": 0}

    def record(self, *, side: str, cex_p_up: float, poly_yes: float, fair: Optional[float],
               outcome_up: bool, bucket: Optional[str] = None,
               context_keys: Optional[list] = None) -> None:
        """Grade one signalled window at close across ALL its context keys (divergence + composite
        microstructure). ``side`` is the signalled side; ``outcome_up`` is the realized result
        (close >= open). Hypothetical PnL is a unit stake at the market price for ``side``."""
        keys = list(context_keys) if context_keys else ([bucket] if bucket else [])
        keys = [k for k in keys if k not in (None, "na", "no_signal")]
        if not keys:
            return
        self.graded += 1
        o = 1.0 if outcome_up else 0.0
        won = (outcome_up and side == "up") or ((not outcome_up) and side == "down")
        price = float(poly_yes) if side == "up" else (1.0 - float(poly_yes))   # price paid for side
        pnl = (1.0 - price) if won else (-price)
        for key in keys:
            s = self.buckets.setdefault(str(key), self._stat())
            s["n"] += 1
            s["correct"] += int(won)
            s["wins"] += int(won)
            s["brier_cex"] = round(s["brier_cex"] + (float(cex_p_up) - o) ** 2, 6)
            s["brier_mkt"] = round(s["brier_mkt"] + (float(poly_yes) - o) ** 2, 6)
            if fair is not None:
                s["brier_fair"] = round(s["brier_fair"] + (float(fair) - o) ** 2, 6)
            s["pnl"] = round(s["pnl"] + pnl, 6)
            s["breakeven_sum"] = round(s["breakeven_sum"] + price, 6)
        self._recent.append({"keys": keys, "side": side, "won": won,
                             "cex_p_up": round(float(cex_p_up), 4),
                             "poly_yes": round(float(poly_yes), 4), "pnl": round(pnl, 4)})

    def is_proven_any(self, context_keys) -> bool:
        """True if ANY of the signal's context keys is a Wilson-proven, market-beating bucket."""
        return any(self.is_proven(k) for k in (context_keys or []))

    def best_proven(self, context_keys):
        """The proven context key with the most samples (for reporting / which rule fired)."""
        proven = [self._assess(k) for k in (context_keys or []) if self.is_proven(k)]
        proven.sort(key=lambda a: -(a.get("n") or 0))
        return proven[0]["bucket"] if proven else None

    def _assess(self, b: str) -> dict:
        s = self.buckets.get(b)
        if not s or s["n"] == 0:
            return {"bucket": b, "n": 0}
        n = s["n"]
        acc = s["correct"] / n
        wl = _wilson_lower(s["correct"], n, self.confidence_z)
        breakeven = s["breakeven_sum"] / n          # avg price paid = win-rate needed to break even
        bc = s["brier_cex"] / n
        bm = s["brier_mkt"] / n
        avg_pnl = s["pnl"] / n
        beats_market = (bm - bc) >= self.min_edge_vs_market
        proven = (n >= self.min_samples and wl is not None and wl > breakeven
                  and beats_market and avg_pnl > self.ev_margin)
        return {"bucket": b, "n": n, "accuracy": round(acc, 4),
                "win_rate_lower_ci": (round(wl, 4) if wl is not None else None),
                "breakeven": round(breakeven, 4),
                "brier_cex": round(bc, 4), "brier_market": round(bm, 4),
                "brier_fair": (round(s["brier_fair"] / n, 4) if s["brier_fair"] else None),
                "beats_market": bool(beats_market), "avg_pnl_per_trade": round(avg_pnl, 4),
                "total_pnl_usd_unit": round(s["pnl"], 4), "proven": bool(proven)}

    def is_proven(self, bucket: Optional[str]) -> bool:
        if not self.enabled or bucket in (None, "na", "no_signal"):
            return False
        return bool(self._assess(bucket).get("proven"))

    # ---------------------------------- driving (gated) ------------------------------------ #
    def decide(self, *, cex_p_up: Optional[float], poly_yes: Optional[float],
               fair: Optional[float] = None, ttc_s: Optional[float] = None,
               basket_direction: Optional[str] = None, exchange_agreement: Optional[float] = None,
               ob_imbalance: Optional[float] = None, tv_direction: Optional[str] = None,
               tv_strength: Optional[float] = None, news_sentiment: Optional[str] = None) -> Optional[dict]:
        """Return a driven-entry proposal ONLY in gated mode when ANY of the signal's context keys is
        a proven, market-beating bucket; else None. Includes an edge-scaled (fractional-Kelly) size.
        Advisory: the safety floor + execution gate still decide the trade."""
        if not self.enabled or self.mode != "gated":
            return None
        sig = self.signal(cex_p_up=cex_p_up, poly_yes=poly_yes, fair=fair, ttc_s=ttc_s,
                          basket_direction=basket_direction, exchange_agreement=exchange_agreement,
                          ob_imbalance=ob_imbalance, tv_direction=tv_direction, tv_strength=tv_strength,
                          news_sentiment=news_sentiment)
        if not sig.get("has_signal"):
            return None
        fired = self.best_proven(sig.get("context_keys"))
        if fired is None:
            return None
        side = sig["side"]
        p_side = float(cex_p_up) if side == "up" else (1.0 - float(cex_p_up))
        price = float(poly_yes) if side == "up" else (1.0 - float(poly_yes))
        self.drove += 1
        return {"side": side, "p_up": round(float(cex_p_up), 4), "outcome_prob": round(p_side, 4),
                "bucket": sig["bucket"], "fired_context": fired, "confirmed": sig.get("confirmed"),
                "tv_confirms": sig.get("tv_confirms"), "late_decisive": sig.get("late_decisive"),
                "size_frac": round(self.size_fraction(p_side=p_side, price=price), 4),
                "divergence": sig["divergence"], "proven": True}

    # ---------------------------------- report / state ------------------------------------- #
    def report(self) -> dict:
        rows = [self._assess(b) for b in self.buckets if b not in ("na", "no_signal")]
        rows.sort(key=lambda r: (not r.get("proven", False), -(r.get("n") or 0)))
        return {"enabled": self.enabled, "mode": self.mode, "paper_only": True,
                "affects_trading": (self.enabled and self.mode == "gated"),
                "min_samples": self.min_samples, "min_divergence": self.min_divergence,
                "confidence_z": self.confidence_z, "tv_strength_thr": self.tv_strength_thr,
                "decisive_thr": self.decisive_thr, "late_ttc_s": self.late_ttc_s,
                "kelly_scale": self.kelly_scale, "max_size_frac": self.max_size_frac,
                "graded": self.graded,
                "signals_seen": self.signals_seen, "drove_entries": self.drove,
                "promotion_rule": ("n>=min AND wilson_lower(win_rate)>breakeven AND "
                                   "Brier_cex<Brier_market AND avg_pnl>0"),
                "any_proven": any(r.get("proven") for r in rows),
                "buckets": rows,
                "note": ("CEX-lead latency edge; benchmark is the MARKET price (already ~0.21 "
                         "Brier). SHADOW grades only; GATED can PROPOSE a side on a proven bucket "
                         "but the execution-quality gate + safety floor remain authoritative. "
                         "PAPER ONLY.")}

    def to_state(self) -> dict:
        return {"buckets": {b: dict(s) for b, s in self.buckets.items()},
                "graded": self.graded, "signals_seen": self.signals_seen, "drove": self.drove}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.buckets = {}
        for b, s in (data.get("buckets") or {}).items():
            st = self._stat()
            for k in st:
                st[k] = (int(s.get(k, 0)) if k in ("n", "correct", "wins")
                         else float(s.get(k, 0.0) or 0.0))
            self.buckets[b] = st
        self.graded = int(data.get("graded", 0) or 0)
        self.signals_seen = int(data.get("signals_seen", 0) or 0)
        self.drove = int(data.get("drove", 0) or 0)
