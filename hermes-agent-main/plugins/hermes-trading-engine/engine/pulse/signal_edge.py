"""Unified signal-edge ledger (WS1) — grade every directional signal vs realized outcomes and emit
a FOLLOW / FADE / OBSERVE verdict per source/context using Wilson lower/upper confidence bounds.

OBSERVE-ONLY by construction: nothing here places, sizes, or bypasses a trade. It surfaces MEASURED
edge so the operator (and, once the freeze lifts + a verdict is promoted) can act on it.

Honest framing the live report already supports: TV composite (~0.44), RSI trend (~0.43) and the
Grok P(up) predictor (~0.40) are all BELOW 0.5 — negative alpha. A signal whose Wilson UPPER bound is
still < 0.5 is CONFIDENTLY anti-predictive, which is itself an exploitable edge: FADE it (trade the
inverse). A signal is only ever promoted to FOLLOW/FADE after n >= min_samples AND the relevant Wilson
bound clears breakeven — never on a small, lucky sample.
"""

from __future__ import annotations

import math
from typing import Optional

FOLLOW = "promote_follow"
FADE = "promote_fade"
OBSERVE = "observe"


def wilson_bounds(wins: int, n: int, *, z: float = 1.96) -> "tuple[float, float]":
    """Wilson score interval for a binomial proportion wins/n. Returns (lower, upper) in [0,1].

    Unknown (n<=0) is the widest possible interval (0,1) so it can never clear a promotion gate.
    """
    n = int(n or 0)
    if n <= 0:
        return (0.0, 1.0)
    p = max(0.0, min(1.0, float(wins) / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt((p * (1.0 - p) + z2 / (4 * n)) / n)) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def classify_signal(n, accuracy, *, min_samples: int = 50, breakeven: float = 0.5,
                    margin: float = 0.0) -> dict:
    """FOLLOW / FADE / OBSERVE for a directional signal from its hit-rate over n settled outcomes.

    - FOLLOW  iff n>=min_samples AND wilson_lower  >  breakeven + margin   (reliably right)
    - FADE    iff n>=min_samples AND wilson_upper  <  (1-breakeven) - margin (reliably wrong -> inverse)
    - OBSERVE otherwise (too few samples, or the interval straddles breakeven)
    """
    n = int(n or 0)
    acc = float(accuracy or 0.0)
    wins = int(round(acc * n))
    lo, hi = wilson_bounds(wins, n)
    fade_bar = (1.0 - breakeven) - margin
    follow_bar = breakeven + margin
    if n < int(min_samples):
        verdict, reason = OBSERVE, "insufficient_samples (n=%d < %d)" % (n, int(min_samples))
    elif lo > follow_bar:
        verdict, reason = FOLLOW, "wilson_lo %.3f > %.3f" % (lo, follow_bar)
    elif hi < fade_bar:
        verdict, reason = FADE, "wilson_hi %.3f < %.3f (confidently anti-predictive)" % (hi, fade_bar)
    else:
        verdict, reason = OBSERVE, "inconclusive (wilson %.3f-%.3f straddles breakeven)" % (lo, hi)
    return {"n": n, "accuracy": round(acc, 4), "wins": wins,
            "wilson_lo": lo, "wilson_hi": hi, "verdict": verdict, "reason": reason,
            "min_samples": int(min_samples), "breakeven": breakeven, "affects_trading": False}


def build_signal_edge_summary(entries: list, *, min_samples: int = 50,
                              breakeven: float = 0.5) -> dict:
    """Aggregate already-graded signal stats into per-source verdicts + follow/fade candidate lists.

    ``entries``: list of {"source", "context"(opt), "n", "accuracy", "avg_pnl"(opt)} — typically
    extracted from the live report's external-signal sections (predictor, TV, RSI, CEX-lead, decider).
    Pure + OBSERVE-ONLY: consumes existing grades, computes Wilson verdicts; never trades.
    """
    sources: dict = {}
    for e in (entries or []):
        src = str(e.get("source") or "?")
        ctx = str(e.get("context") or "all")
        if e.get("n") is None or e.get("accuracy") is None:
            continue
        cl = classify_signal(e.get("n"), e.get("accuracy"),
                             min_samples=min_samples, breakeven=breakeven)
        if e.get("avg_pnl") is not None:
            cl["avg_pnl"] = e.get("avg_pnl")
        sources.setdefault(src, {})[ctx] = cl

    def _pick(verdict):
        return [{"source": s, "context": c, "n": v["n"], "accuracy": v["accuracy"],
                 "wilson_lo": v["wilson_lo"], "wilson_hi": v["wilson_hi"]}
                for s, d in sources.items() for c, v in d.items() if v["verdict"] == verdict]

    return {
        "observe_only": True, "affects_trading": False, "min_samples": int(min_samples),
        "breakeven": breakeven, "sources": sources,
        "fade_candidates": _pick(FADE),
        "follow_candidates": _pick(FOLLOW),
        "note": ("FADE = confidently anti-predictive (Wilson upper < breakeven) -> trade the inverse "
                 "(gated, never auto); FOLLOW = Wilson lower > breakeven; else OBSERVE. Measured on "
                 "real settled outcomes; never places/sizes/bypasses a trade."),
    }


def _num(*vals):
    for v in vals:
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def extract_signal_edge_entries(*, tradingview: Optional[dict] = None,
                                grok_decider: Optional[dict] = None,
                                grok_signal_intel: Optional[dict] = None,
                                cex_lead_edge: Optional[dict] = None) -> list:
    """Normalize the live report's already-graded signal stats into signal-edge entries.

    Defensive: any source missing n or accuracy is simply skipped. Used by reporting.py to surface
    the unified FOLLOW/FADE/OBSERVE verdicts without touching the (frozen) engine grading.
    """
    tv = tradingview or {}
    gd = grok_decider or {}
    gsi = grok_signal_intel or {}
    cex = cex_lead_edge or {}
    out: list = []

    def add(source, n, acc, *, context="all", avg_pnl=None):
        n = _num(n)
        acc = _num(acc)
        if n is None or acc is None or n <= 0:
            return
        out.append({"source": source, "context": context, "n": int(n),
                    "accuracy": acc, "avg_pnl": avg_pnl})

    # Grok P(up) predictor (predictor_B): observe-only graded P(up) vs realized.
    pb = gsi.get("predictor_B") if isinstance(gsi, dict) else None
    if isinstance(pb, dict):
        add("grok_predictor", pb.get("scored") or pb.get("graded"), pb.get("accuracy"))

    # TradingView RSI-trend state predictions.
    rsi = tv.get("rsi_trend") if isinstance(tv, dict) else None
    if isinstance(rsi, dict):
        add("rsi_trend", rsi.get("n") or rsi.get("samples") or rsi.get("signals_evaluated"),
            rsi.get("hit_rate") or rsi.get("accuracy"))

    # TradingView composite vs realized 5-min outcome.
    edge5 = tv.get("edge_vs_5min_outcome") if isinstance(tv, dict) else None
    if isinstance(edge5, dict):
        add("tradingview_composite", edge5.get("n_settled_with_signal") or edge5.get("n"),
            edge5.get("signal_hit_rate"))

    # Grok decider directional view (headline + per-context).
    add("grok_decider", gd.get("decided") or gd.get("views_graded"), gd.get("direction_accuracy"))
    for ctx, row in (gd.get("accuracy_by_context") or {}).items() if isinstance(gd, dict) else []:
        if isinstance(row, dict):
            for bucket, st in row.items():
                if isinstance(st, dict):
                    add("grok_decider", st.get("n"), st.get("accuracy"),
                        context="%s=%s" % (ctx, bucket))

    # CEX-lead latency edge, per divergence/context bucket.
    rows = cex.get("buckets") or cex.get("rows") if isinstance(cex, dict) else None
    if isinstance(rows, dict):
        for bucket, st in rows.items():
            if isinstance(st, dict):
                add("cex_lead", st.get("n"), st.get("acc") or st.get("accuracy"),
                    context=str(bucket), avg_pnl=st.get("avg_pnl") or st.get("avg_pnl_per_unit"))
    return out


class SignalEdgeLedger:
    """Durable live accumulator of (source, context) signal predictions vs realized outcomes.

    Wired at settlement (observe-only) when the engine is unfrozen; ``report()`` emits the same
    verdict surface as ``build_signal_edge_summary``. Persisted via to_state/load_state.
    """

    def __init__(self, *, min_samples: int = 50, breakeven: float = 0.5):
        self.min_samples = int(min_samples)
        self.breakeven = float(breakeven)
        self.cells: dict = {}            # "source|context" -> {"n","wins","pnl"}

    @staticmethod
    def _key(source: str, context: str) -> str:
        return "%s|%s" % (source, context)

    def record(self, source: str, *, predicted_up: Optional[bool], outcome_up: Optional[bool],
               context: str = "all", pnl_if_followed: float = 0.0) -> None:
        if predicted_up is None or outcome_up is None:
            return
        c = self.cells.setdefault(self._key(str(source), str(context)),
                                  {"n": 0, "wins": 0, "pnl": 0.0})
        c["n"] += 1
        c["wins"] += int(bool(predicted_up) == bool(outcome_up))
        c["pnl"] = round(c["pnl"] + float(pnl_if_followed or 0.0), 6)

    def _entries(self) -> list:
        out = []
        for key, c in self.cells.items():
            src, _, ctx = key.partition("|")
            n = c["n"]
            out.append({"source": src, "context": ctx, "n": n,
                        "accuracy": (c["wins"] / n if n else None),
                        "avg_pnl": (round(c["pnl"] / n, 6) if n else None)})
        return out

    def report(self) -> dict:
        rep = build_signal_edge_summary(self._entries(), min_samples=self.min_samples,
                                        breakeven=self.breakeven)
        rep["strategy"] = "signal_edge_ledger"
        return rep

    def to_state(self) -> dict:
        return {"min_samples": self.min_samples, "breakeven": self.breakeven,
                "cells": {k: dict(v) for k, v in self.cells.items()}}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.min_samples = int(data.get("min_samples", self.min_samples) or self.min_samples)
        self.breakeven = float(data.get("breakeven", self.breakeven) or self.breakeven)
        self.cells = {}
        for k, v in (data.get("cells") or {}).items():
            if isinstance(v, dict):
                self.cells[k] = {"n": int(v.get("n", 0) or 0), "wins": int(v.get("wins", 0) or 0),
                                 "pnl": float(v.get("pnl", 0.0) or 0.0)}
