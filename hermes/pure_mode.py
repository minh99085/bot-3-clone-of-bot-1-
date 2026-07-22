"""PURE mode — barrier-vs-market with the adaptive stack stripped (B1).

The 10-lane experiment and the honest-edge question both need a run mode with
NO confounds: the strategy is exactly "barrier q vs market p through the frozen
entry gates", sizing is FIXED, and every self-adjusting layer is off:

  - bandit (explore/exploit/skip arms + reward updates)     → fixed neutral arm
  - lessons engine (AVOID/CUT rules + new lesson writes)    → not read, not written
  - risk-monitor pauses / circuit breaker                   → observed, never trips
  - MCHB hierarchical gate, RGMC soft sizing, autonomy hooks→ no-ops

Enable per container with ``HERMES_PURE_MODE=1``. Fixed size is
``HERMES_PURE_SIZE_PCT`` of bankroll (default 2%, always clamped by the
``HERMES_MAX_TRADE_PCT`` hard cap in pretrade).

Two uses:
  1. Autonomy A/B (problem #5): lane01_baseline (pure) vs lane02_autonomy
     (identical barrier q, full adaptive stack). Same market stream → the PnL
     difference IS the autonomy layer's net contribution.
  2. Lane experiment validity (problem #4): every q-mode lane runs pure, so
     lanes differ ONLY in q — per-lane adaptive state can no longer diverge
     sizing/entries and invalidate the paired scoreboard.

Safety rails that are NOT disabled: the paper-only lock, market scope checks,
the per-trade hard cap, and the frozen entry gates (autonomy/freeze.py).
"""

from __future__ import annotations

import os

ENV_FLAG = "HERMES_PURE_MODE"
ENV_SIZE = "HERMES_PURE_SIZE_PCT"
DEFAULT_SIZE_PCT = 0.02  # 2% of bankroll — matches the hard per-trade cap


def pure_mode_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "0").strip().lower() in ("1", "true", "yes")


def pure_fixed_size_pct() -> float:
    """Fixed per-trade fraction of bankroll (clamped to a sane [0.1%, 5%])."""
    try:
        pct = float(os.environ.get(ENV_SIZE, str(DEFAULT_SIZE_PCT)))
    except ValueError:
        pct = DEFAULT_SIZE_PCT
    return min(0.05, max(0.001, pct))
