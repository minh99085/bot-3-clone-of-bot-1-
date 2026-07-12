#!/usr/bin/env python3
"""Run the BTC 5-minute "Up or Down" pulse PAPER engine.

This is the focused entrypoint: the bot trades ONLY the Polymarket ``btc-up-or-down-5m``
series, in paper mode. It HARD-REFUSES to start if any live-execution flag is set.

    python scripts/run_btc_pulse.py
    python scripts/run_btc_pulse.py --max-ticks 3        # smoke test
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_FORBIDDEN_LIVE_FLAGS = (
    "LIVE_TRADING_ENABLED", "POLYMARKET_LIVE_ENABLED", "POLYMARKET_LIVE_TRADING_ENABLED",
    "POLYMARKET_AUTOTRADE_ENABLED", "BTC_AUTOTRADE_ENABLED", "BTC_PULSE_LIVE_ENABLED",
    "GUARDED_LIVE_ENABLED", "MICRO_LIVE_ENABLED", "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION",
    "ARB_EXECUTION_ENABLED", "MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK",
)


def _preflight() -> None:
    """Abort if any live-execution flag is truthy. PAPER ONLY, always."""
    bad = [f for f in _FORBIDDEN_LIVE_FLAGS
           if str(os.getenv(f, "")).strip().lower() in ("1", "true", "yes", "on")]
    if bad:
        raise SystemExit(f"REFUSING TO START: live-execution flag(s) set: {bad}. "
                         "The BTC pulse engine is PAPER ONLY.")


def main() -> int:
    ap = argparse.ArgumentParser(description="BTC 5-min pulse paper engine (PAPER ONLY)")
    ap.add_argument("--max-ticks", type=int, default=None)
    args = ap.parse_args()

    logging.basicConfig(level=os.getenv("HTE_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if "pytest" not in sys.modules:
            from engine.env_loader import load_local_env
            load_local_env()                  # strips forbidden live flags on load
    except Exception:  # noqa: BLE001
        pass
    _preflight()

    from engine.pulse.engine import PulseEngine, PulseConfig
    eng = PulseEngine(PulseConfig.from_env())
    print("BTC 5-min pulse engine: PAPER ONLY, live trading OFF. Trading "
          "btc-up-or-down-5m windows only.")
    eng.run(max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
