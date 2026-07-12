"""PRISM — Posterior-Ranked Information State Machine (PAPER ONLY).

PRISM ranks Polymarket crypto 1h up/down opportunities with a single scalar::

    R = I * max(0, E) * C

  I = information completeness [0,1]  (signals arrived x freshness)
  E = ensemble Monte-Carlo edge vs ask after slippage
  C = confidence [0,1]               (model agreement x bucket posterior)

The package is built one phase at a time (see docs/PRISM_CURSOR_PHASES.txt). Every module is
PAPER ONLY and observe-only until the final integration phase: PRISM may restrict or size a
paper trade, but it can never force a fill or bypass ``execution_gate.py``.
"""

from __future__ import annotations

__all__ = ["belief"]
