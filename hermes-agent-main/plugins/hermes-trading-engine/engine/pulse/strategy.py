"""Loosened decision model for the BTC 5-min pulse (PAPER ONLY).

Given the digital fair value ``P(up)`` and the live Up/Down books, pick the side with the
larger positive after-cost edge and decide whether to take a PAPER position. The quality
gates here are intentionally LOOSE (small min-edge, shallow depth) per the operator
directive — they only affect which *paper* trades are taken; nothing here can place a real
order. The HARD safety limits that remain: never trade a closed window, never trade without
a live ask, never trade after the open snapshot is missing/late, never pay above a price cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class PulseDecision:
    trade: bool
    side: Optional[str] = None          # "up" | "down"
    token_id: Optional[str] = None
    price: Optional[float] = None       # marketable ask we'd pay (paper)
    fair_p_up: Optional[float] = None
    edge: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {"trade": self.trade, "side": self.side, "price": self.price,
                "fair_p_up": (round(self.fair_p_up, 4) if self.fair_p_up is not None else None),
                "edge": round(self.edge, 4), "reason": self.reason}


def decide(window, fair_p_up: Optional[float], now: float, *,
           min_edge: float = 0.03, min_seconds_to_close: float = 4.0,
           min_depth_usd: float = 1.0, edge_buffer: float = 0.01,
           max_price: float = 0.97, min_seconds_since_open: float = 0.0,
           basis_buffer: float = 0.0, min_reward_risk: float = 0.0,
           min_reward_risk_up: Optional[float] = None,
           force_side: Optional[str] = None) -> PulseDecision:
    """Return the PAPER trade decision for ``window`` at time ``now``.

    Quality gates that protect EXPECTANCY (not just realism): skip the dead early window
    (``min_seconds_since_open`` — before a real move develops the digital is ~0.5 noise) and
    require the after-cost edge to clear ``edge_buffer + basis_buffer`` (the basis buffer
    covers the Coinbase-vs-Chainlink-resolution drift, our dominant correctness risk)."""
    if fair_p_up is None:
        return PulseDecision(False, reason="no_fair_value")
    ttc = window.seconds_to_close(now)
    if ttc <= min_seconds_to_close:
        return PulseDecision(False, fair_p_up=fair_p_up, reason="too_close_to_settlement")
    if window.seconds_since_open(now) < min_seconds_since_open:
        return PulseDecision(False, fair_p_up=fair_p_up, reason="too_early_in_window")
    buf = float(edge_buffer) + float(basis_buffer)
    up_b, dn_b = window.up_book, window.down_book
    up_ask = up_b.best_ask if up_b else None
    dn_ask = dn_b.best_ask if dn_b else None
    up_depth = up_b.ask_depth_usd if up_b else 0.0
    dn_depth = dn_b.ask_depth_usd if dn_b else 0.0
    # after-cost edge for each side: P(outcome) - ask_paid - buffer (basis-drift/open-lag)
    cand = []
    if up_ask is not None and up_ask <= max_price and up_depth >= min_depth_usd:
        cand.append(("up", window.up_token_id, float(up_ask),
                     fair_p_up - float(up_ask) - buf))
    if dn_ask is not None and dn_ask <= max_price and dn_depth >= min_depth_usd:
        cand.append(("down", window.down_token_id, float(dn_ask),
                     (1.0 - fair_p_up) - float(dn_ask) - buf))
    if not cand:
        return PulseDecision(False, fair_p_up=fair_p_up, reason="no_tradeable_ask")
    want = str(force_side or "").strip().lower()
    if want in ("up", "down"):
        picked = [c for c in cand if c[0] == want]
        if not picked:
            return PulseDecision(False, fair_p_up=fair_p_up, reason="no_tradeable_ask")
        side, token, price, edge = picked[0]
    else:
        side, token, price, edge = max(cand, key=lambda c: c[3])
    if edge < min_edge:
        return PulseDecision(False, side=side, token_id=token, price=price,
                             fair_p_up=fair_p_up, edge=edge, reason="edge_below_min")
    # reward-to-risk floor: at ask ``price`` a winning $1 stake nets ``(1-price)/price`` while a loss
    # costs the full stake. High-price entries (e.g. 0.91 -> ~0.10 reward/risk: win ~$0.49 vs risk
    # $5) are skipped — one loss wipes ~10 such wins, and they are fragile to model miscalibration.
    rr_floor = float(min_reward_risk)
    if side == "up" and min_reward_risk_up is not None and float(min_reward_risk_up) > rr_floor:
        rr_floor = float(min_reward_risk_up)
    if rr_floor > 0.0 and price is not None and price > 0.0:
        reward_risk = (1.0 - float(price)) / float(price)
        if reward_risk < rr_floor:
            return PulseDecision(False, side=side, token_id=token, price=price,
                                 fair_p_up=fair_p_up, edge=edge, reason="reward_risk_too_low")
    return PulseDecision(True, side=side, token_id=token, price=price,
                         fair_p_up=fair_p_up, edge=edge, reason="trade")
