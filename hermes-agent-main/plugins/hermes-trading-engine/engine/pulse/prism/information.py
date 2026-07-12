"""PRISM Phase 2 — Information Completeness tracker + hour-timing FSM (PAPER ONLY).

Information completeness ``I(t)`` in [0, 1] measures how much of the expected hourly evidence has
arrived *and* is still fresh::

    I(t) = sum_i( w_i * freshness_i(t) * observed_i ) / sum_i( w_i )

Weights and freshness half-lives come from :data:`belief.DEFAULT_SIGNAL_WEIGHTS`. ``I(t)`` gates
Sniper mode (needs ``I >= 0.70``) together with the hour-timing FSM. This module is observe-only:
it never authorizes a trade; a thin read-only hook publishes ``prism_information`` to the status API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from engine.pulse.prism.belief import (
    DEFAULT_SIGNAL_WEIGHTS,
    SignalObservation,
    freshness,
    make_observation,
)

# Signal is treated as "missing" once its freshness decays below this floor.
_FRESH_FLOOR = 0.10

# When each signal is *expected* to have arrived within the hour (minute of the window). Used as the
# optimal-stopping baseline curve — how complete the information set should be by a given minute.
EXPECTED_ARRIVAL_MINUTE: dict[str, float] = {
    "chainlink_anchor": 0.0,
    "quant_fair": 0.0,
    "cex_lead": 1.0,
    "book_imbalance": 1.0,
    "tv_15m": 15.0,
    "tv_30m": 30.0,
    "tv_45m": 45.0,
    "tv_60m": 60.0,
    "tv_240m": 240.0,
    "tv_1440m": 1440.0,
}


class FSMState(str, Enum):
    """Hour-timing finite state machine (seconds since the window opened)."""

    WATCHING = "watching"          # < 180s — too early, gather info
    TIER1_READY = "tier1_ready"    # 180s – 12m — harvester-eligible
    TIER2_CONFIRM = "tier2_confirm"  # 12m – 35m — sniper-eligible
    LATE_WINDOW = "late_window"    # 35m – 50m — sniper-eligible (nowcast)
    EXPIRED = "expired"            # > 50m — no new entries


# FSM boundaries in seconds since open.
_WATCHING_MAX_S = 180.0
_TIER1_MAX_S = 12 * 60.0        # 720
_TIER2_MAX_S = 35 * 60.0        # 2100
_LATE_MAX_S = 50 * 60.0         # 3000

# Tiers that may run a Sniper entry.
_SNIPER_STATES = (FSMState.TIER2_CONFIRM, FSMState.LATE_WINDOW)


def state_from_seconds_since_open(sso: Optional[float]) -> FSMState:
    """Map seconds-since-open to an :class:`FSMState`."""
    if sso is None:
        return FSMState.WATCHING
    try:
        s = float(sso)
    except (TypeError, ValueError):
        return FSMState.WATCHING
    if s < _WATCHING_MAX_S:
        return FSMState.WATCHING
    if s < _TIER1_MAX_S:
        return FSMState.TIER1_READY
    if s < _TIER2_MAX_S:
        return FSMState.TIER2_CONFIRM
    if s < _LATE_MAX_S:
        return FSMState.LATE_WINDOW
    return FSMState.EXPIRED


def max_tier_for_state(state: FSMState) -> str:
    """Highest agent tier allowed in a given FSM state. Sniper only in TIER2_CONFIRM/LATE_WINDOW."""
    if state in _SNIPER_STATES:
        return "sniper"
    if state == FSMState.TIER1_READY:
        return "harvester"
    return "none"


def _direction_from_tv(raw_dir) -> int:
    d = str(raw_dir or "").strip().upper()
    if d == "UP":
        return 1
    if d == "DOWN":
        return -1
    return 0


@dataclass
class InformationTracker:
    """Tracks which signals have arrived (and when) for one window; computes ``I(t)``.

    Configure from :data:`belief.DEFAULT_SIGNAL_WEIGHTS`. Observe-only.
    """

    weights: dict = field(default_factory=lambda: dict(DEFAULT_SIGNAL_WEIGHTS))
    received_at: dict = field(default_factory=dict)  # signal_name -> epoch seconds

    def observe(self, signal_name: str, received_at: float, now: Optional[float] = None) -> None:
        """Mark ``signal_name`` as received at ``received_at`` (epoch seconds)."""
        if signal_name in self.weights:
            self.received_at[signal_name] = float(received_at)

    def reset(self) -> None:
        self.received_at.clear()

    def _weight(self, name: str) -> float:
        return float((self.weights.get(name) or {}).get("weight", 0.0))

    def _half_life(self, name: str) -> Optional[float]:
        return (self.weights.get(name) or {}).get("half_life_s")

    def freshness_at(self, signal_name: str, now: float) -> float:
        """Current freshness [0,1] of a signal (0 if never observed)."""
        ts = self.received_at.get(signal_name)
        if ts is None:
            return 0.0
        return freshness(float(now) - float(ts), self._half_life(signal_name))

    def completeness(self, now: float) -> float:
        """Information completeness ``I(t)`` in [0, 1]."""
        total_w = sum(self._weight(n) for n in self.weights)
        if total_w <= 0:
            return 0.0
        acc = 0.0
        for name in self.weights:
            ts = self.received_at.get(name)
            if ts is None:
                continue
            acc += self._weight(name) * self.freshness_at(name, now)
        return max(0.0, min(1.0, acc / total_w))

    def expected_completeness_at_minute(self, minute: float) -> float:
        """Baseline ``I`` typically available by ``minute`` of the window (optimal-stopping ref)."""
        total_w = sum(self._weight(n) for n in self.weights)
        if total_w <= 0:
            return 0.0
        acc = sum(self._weight(n) for n in self.weights
                  if EXPECTED_ARRIVAL_MINUTE.get(n, 60.0) <= float(minute))
        return max(0.0, min(1.0, acc / total_w))

    def missing_signals(self, now: float) -> list:
        """Signals never observed or decayed below the freshness floor."""
        out = []
        for name in self.weights:
            if self.received_at.get(name) is None or self.freshness_at(name, now) < _FRESH_FLOOR:
                out.append(name)
        return out

    def is_sniper_eligible(self, now: float, sso: Optional[float] = None,
                           i_floor: float = 0.70) -> bool:
        """Sniper needs ``I >= i_floor`` AND (if ``sso`` given) an FSM state in TIER2/LATE."""
        if self.completeness(now) < float(i_floor):
            return False
        if sso is None:
            return True
        return state_from_seconds_since_open(sso) in _SNIPER_STATES

    def to_report(self, now: float, sso: Optional[float] = None) -> dict:
        """Observe-only summary for the status API / dashboard."""
        state = state_from_seconds_since_open(sso) if sso is not None else None
        return {
            "enabled": True,
            "I": round(self.completeness(now), 4),
            "n_observed": len(self.received_at),
            "freshness": {n: round(self.freshness_at(n, now), 4)
                          for n in self.weights if self.received_at.get(n) is not None},
            "missing": self.missing_signals(now),
            "fsm_state": state.value if state is not None else None,
            "sniper_max_tier": max_tier_for_state(state) if state is not None else None,
            "sniper_eligible": self.is_sniper_eligible(now, sso),
            "sso_s": round(float(sso), 1) if sso is not None else None,
        }


# --------------------------------------------------------------------------------------------- #
# Ingest helpers — turn raw engine data into belief.SignalObservation and mark the tracker.
# All return None (or empty) when there is no usable signal; never raise.
# --------------------------------------------------------------------------------------------- #

def ingest_tv_latest(tradingview_latest_by_timeframe: dict, symbol: str, now: float,
                     *, timeframes=("5", "15", "30", "45", "60", "240", "1440"),
                     tracker: Optional[InformationTracker] = None) -> list:
    """Map ``{symbol}@{tf}`` TV snapshots to ``tv_<tf>m`` :class:`SignalObservation` objects.

    If a ``tracker`` is supplied, each fresh directional alert is also ``observe``-d on it.
    """
    obs: list = []
    lbt = tradingview_latest_by_timeframe or {}
    for tf in timeframes:
        snap = lbt.get("%s@%s" % (symbol, tf)) or {}
        direction = _direction_from_tv(snap.get("direction"))
        ts = snap.get("ts")
        if direction == 0 or ts is None:
            continue
        name = "tv_%sm" % tf
        strength = snap.get("strength")
        strength = float(strength) if strength is not None else 0.5
        age_s = max(0.0, float(now) - float(ts))
        o = make_observation(name, direction, strength, age_s)
        obs.append(o)
        if tracker is not None:
            tracker.observe(name, float(ts), now)
    return obs


def ingest_cex_lead(cex_snapshot: Optional[dict], now: float,
                    *, tracker: Optional[InformationTracker] = None) -> Optional[SignalObservation]:
    """Build a ``cex_lead`` observation from a CEX-lead feature dict (or None)."""
    if not cex_snapshot:
        return None
    p_up = cex_snapshot.get("cex_p_up", cex_snapshot.get("p_up"))
    momentum = cex_snapshot.get("cex_momentum", cex_snapshot.get("momentum"))
    if p_up is not None:
        try:
            edge = float(p_up) - 0.5
        except (TypeError, ValueError):
            return None
        direction = 1 if edge > 0 else (-1 if edge < 0 else 0)
        strength = min(1.0, abs(edge) * 2.0)
    elif momentum is not None:
        try:
            m = float(momentum)
        except (TypeError, ValueError):
            return None
        direction = 1 if m > 0 else (-1 if m < 0 else 0)
        strength = min(1.0, abs(m))
    else:
        return None
    if direction == 0 or strength <= 0.0:
        return None
    ts = cex_snapshot.get("ts", now)
    age_s = max(0.0, float(now) - float(ts))
    if tracker is not None:
        tracker.observe("cex_lead", float(ts), now)
    return make_observation("cex_lead", direction, strength, age_s)


def ingest_book_imbalance(imbalance: Optional[float], spread: Optional[float], now: float,
                          *, tracker: Optional[InformationTracker] = None
                          ) -> Optional[SignalObservation]:
    """Build a ``book_imbalance`` observation from top-of-book imbalance in [-1, 1]."""
    if imbalance is None:
        return None
    try:
        imb = float(imbalance)
    except (TypeError, ValueError):
        return None
    direction = 1 if imb > 0 else (-1 if imb < 0 else 0)
    if direction == 0:
        return None
    strength = min(1.0, abs(imb))
    # A wide spread makes book pressure less trustworthy — shrink strength.
    if spread is not None:
        try:
            sp = float(spread)
            if sp > 0.05:
                strength *= 0.5
        except (TypeError, ValueError):
            pass
    if strength <= 0.0:
        return None
    if tracker is not None:
        tracker.observe("book_imbalance", now, now)
    return make_observation("book_imbalance", direction, strength, 0.0)


def ingest_quant_fair(fair_p: Optional[float], market_ask: Optional[float], now: float,
                      *, tracker: Optional[InformationTracker] = None
                      ) -> Optional[SignalObservation]:
    """Build a ``quant_fair`` observation from the closed-form digital fair ``P(up)``."""
    if fair_p is None:
        return None
    try:
        p = float(fair_p)
    except (TypeError, ValueError):
        return None
    edge = p - 0.5
    direction = 1 if edge > 0 else (-1 if edge < 0 else 0)
    if direction == 0:
        return None
    strength = min(1.0, abs(edge) * 2.0)
    if strength <= 0.0:
        return None
    if tracker is not None:
        tracker.observe("quant_fair", now, now)
    return make_observation("quant_fair", direction, strength, 0.0)
