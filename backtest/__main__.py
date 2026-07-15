"""CLI: python -m backtest

Runs synthetic (default) enhanced-misprice backtest and prints the report.
Optionally auto-tightens thresholds until ≥80% WR or rounds exhausted.
"""

from __future__ import annotations

import argparse
import logging
import sys

from backtest.engine import ensure_target_or_tighten, run_backtest
from models.config import load_enhanced_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enhanced misprice backtest")
    parser.add_argument("--config", default="config/enhanced_misprice.yaml")
    parser.add_argument("--historical", action="store_true", help="Use Gamma cache/API")
    parser.add_argument("--auto-tighten", action="store_true", default=True)
    parser.add_argument("--no-auto-tighten", action="store_false", dest="auto_tighten")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_enhanced_config(args.config)

    if args.historical:
        from backtest.historical import load_historical

        markets = load_historical(limit=250)
        result = run_backtest(markets, config=cfg, use_synthetic=False)
    elif args.auto_tighten:
        result = ensure_target_or_tighten(config=cfg)
    else:
        result = run_backtest(config=cfg, use_synthetic=True)

    print(result.report.summary())
    print(f"\nTARGET_MET={result.target_met}  Brier={result.brier:.4f}")
    if result.suggested_stricter:
        print("\n".join(result.suggested_stricter))
    return 0 if result.target_met else 1


if __name__ == "__main__":
    sys.exit(main())
