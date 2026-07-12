"""BTC 5-minute "Up or Down" pulse paper-trading engine (PAPER ONLY).

This package is the focused redesign of the bot: it trades ONLY the Polymarket
``btc-up-or-down-5m`` series in paper mode. It ingests the rolling 5-minute windows,
tracks the window-open BTC reference price, prices each window as a digital option
(P(up) = P(close >= open)), simulates paper fills against the live CLOB book, and
resolves each window for paper P&L + calibration.

HARD SAFETY INVARIANT (never relaxed): this engine NEVER places a real order, signs,
or touches a wallet. Every "fill" is a simulated paper fill. Loosened *quality* gates
(edge size, realism tolerances) only affect which PAPER trades are taken.
"""

from engine.pulse.markets import PulseWindow, PulseMarketFeed
from engine.pulse.fair_value import digital_p_up, RollingVol
from engine.pulse.strategy import PulseDecision, decide

__all__ = [
    "PulseWindow", "PulseMarketFeed", "digital_p_up", "RollingVol",
    "PulseDecision", "decide",
]
