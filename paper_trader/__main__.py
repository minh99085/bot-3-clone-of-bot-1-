"""CLI: python -m paper_trader

Runs the enhanced-misprice async paper loop (no on-chain keys).
For the full Hermes overnight bot prefer: python -m hermes.hermes_loop overnight
"""

from __future__ import annotations

from paper_trader.loop import main

if __name__ == "__main__":
    main()
