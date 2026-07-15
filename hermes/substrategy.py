"""Sub-strategy identity — each unique return source is a portfolio sleeve.

Sub-strategy key =
  market_series + entry_mode + regime + hourly_bucket

Ruuj framing: treat each as a distinct return source for HRP / cut-reduce.
"""

from __future__ import annotations

import re
from typing import Optional

from hermes.models import (
    EntryMode,
    Regime,
    Signal,
    SubStrategyAction,
    SubStrategyConfidence,
)


def infer_market_series(market_id: str, slug: str = "", question: str = "") -> str:
    """Map Polymarket BTC/ETH up-down (and peers) into series labels."""
    blob = f"{market_id} {slug} {question}".lower()
    if re.search(r"\bbtc\b|bitcoin", blob):
        if re.search(r"up.?down|5m|15m|1h|hourly", blob):
            return "btc_updown"
        return "btc"
    if re.search(r"\beth\b|ethereum", blob):
        if re.search(r"up.?down|5m|15m|1h|hourly", blob):
            return "eth_updown"
        return "eth"
    if "fed" in blob or "rate" in blob:
        return "macro_rates"
    if "election" in blob or "vote" in blob:
        return "politics"
    # Synthetic demo ids
    if market_id.startswith("mkt_btc") or "btc" in market_id:
        return "btc_updown"
    if market_id.startswith("mkt_eth") or "eth" in market_id:
        return "eth_updown"
    if market_id.startswith("mkt_fed"):
        return "macro_rates"
    return "misc"


def make_substrategy_id(
    market_series: str,
    entry_mode: EntryMode | str,
    regime: Regime | str,
    hourly_bucket: int,
) -> str:
    mode = entry_mode.value if isinstance(entry_mode, EntryMode) else str(entry_mode)
    reg = regime.value if isinstance(regime, Regime) else str(regime)
    return f"{market_series}|{mode}|{reg}|h{int(hourly_bucket)}"


def annotate_signal(signal: Signal) -> Signal:
    """Fill market_series + substrategy_id on a signal in-place-ish (returns copy fields)."""
    series = signal.market_series if signal.market_series != "unknown" else infer_market_series(
        signal.market_id, signal.slug, signal.question
    )
    sid = signal.substrategy_id or make_substrategy_id(
        series, signal.entry_mode, signal.regime, signal.hourly_bucket
    )
    signal.market_series = series
    signal.substrategy_id = sid
    return signal


def composite_confidence(
    *,
    rolling_ev: float,
    rolling_wr: float,
    wr_trend: float,
    ev_trend: float,
    regime_stability: float,
    brier_score: float,
) -> float:
    """Map internal metrics → [0,1] confidence. Lower brier is better."""
    ev_score = max(0.0, min(1.0, (rolling_ev + 0.05) / 0.15))
    wr_score = max(0.0, min(1.0, (rolling_wr - 0.45) / 0.40))
    trend = max(0.0, min(1.0, 0.5 + wr_trend + ev_trend))
    brier_score_n = max(0.0, min(1.0, 1.0 - (brier_score / 0.5)))
    raw = (
        0.30 * ev_score
        + 0.25 * wr_score
        + 0.20 * trend
        + 0.15 * regime_stability
        + 0.10 * brier_score_n
    )
    return float(max(0.0, min(1.0, raw)))


def decide_action(conf: SubStrategyConfidence) -> SubStrategyConfidence:
    """Chapter 5 cut/reduce: separate currently_losing from model_broken.

    - currently_losing: temporary PnL pain → REDUCE, not necessarily CUT
    - model_broken: rolling EV/WR/brier/regime collapse → CUT even if still +PnL
    """
    notes: list[str] = []
    model_broken = False
    if conf.sample_n >= 15 and conf.rolling_ev < 0.02:
        model_broken = True
        notes.append("rolling_ev_collapsed")
    if conf.sample_n >= 15 and conf.rolling_wr < 0.55 and conf.wr_trend < -0.05:
        model_broken = True
        notes.append("wr_trend_broken")
    if conf.brier_score > 0.35 and conf.sample_n >= 20:
        model_broken = True
        notes.append("brier_uninformative")
    if conf.regime_stability < 0.4:
        model_broken = True
        notes.append("regime_unstable")
    # Toxic modes when degrading
    if conf.entry_mode == EntryMode.OSMANI_LANE and (
        conf.rolling_ev < 0.05 or conf.internal_confidence < 0.45
    ):
        model_broken = True
        notes.append("osmani_degrading")

    conf.model_broken = model_broken

    if model_broken:
        conf.action = SubStrategyAction.CUT
        conf.weight_cap = 0.0
        conf.notes = "; ".join(notes) or "model_broken"
        return conf

    if conf.internal_confidence < 0.40 or (
        conf.currently_losing and conf.ev_trend < 0
    ):
        conf.action = SubStrategyAction.REDUCE
        conf.weight_cap = min(conf.weight_cap, 0.08)
        conf.notes = conf.notes or "degrading_confidence_reduce"
        return conf

    if (
        conf.internal_confidence >= 0.75
        and conf.ev_trend > 0
        and conf.wr_trend >= 0
        and not conf.currently_losing
    ):
        conf.action = SubStrategyAction.BOOST
        conf.weight_cap = min(0.30, max(conf.weight_cap, 0.20))
        conf.notes = "rising_confidence_boost"
        return conf

    conf.action = SubStrategyAction.HOLD
    conf.weight_cap = min(0.25, max(0.05, conf.weight_cap))
    conf.notes = conf.notes or "hold"
    return conf


def default_confidence(substrategy_id: str, signal: Optional[Signal] = None) -> SubStrategyConfidence:
    """Cold-start prior for a sleeve before enough settlements exist."""
    parts = substrategy_id.split("|")
    series = parts[0] if parts else "unknown"
    mode = EntryMode.MEAN_REVERSION
    regime = Regime.UNKNOWN
    hour = 0
    try:
        if len(parts) >= 2:
            mode = EntryMode(parts[1])
        if len(parts) >= 3:
            regime = Regime(parts[2])
        if len(parts) >= 4:
            hour = int(parts[3].lstrip("h"))
    except ValueError:
        pass
    if signal is not None:
        series = signal.market_series
        mode = signal.entry_mode
        regime = signal.regime
        hour = signal.hourly_bucket
    # Seed priors from signal quality when available
    rolling_ev = float(signal.live_ev) if signal else 0.06
    rolling_wr = 0.70 if signal and signal.confidence_tier.value in ("A", "B") else 0.60
    conf = SubStrategyConfidence(
        substrategy_id=substrategy_id,
        market_series=series,
        entry_mode=mode,
        regime=regime,
        hourly_bucket=hour,
        sample_n=0,
        rolling_ev=rolling_ev,
        rolling_wr=rolling_wr,
        wr_trend=0.0,
        ev_trend=0.0,
        regime_stability=0.8,
        brier_score=0.22,
        weight_cap=0.20,
    )
    conf.internal_confidence = composite_confidence(
        rolling_ev=conf.rolling_ev,
        rolling_wr=conf.rolling_wr,
        wr_trend=conf.wr_trend,
        ev_trend=conf.ev_trend,
        regime_stability=conf.regime_stability,
        brier_score=conf.brier_score,
    )
    return decide_action(conf)
