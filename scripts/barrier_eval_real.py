#!/usr/bin/env python3
"""A3 GO/NO-GO — does barrier_q beat the market on REAL Chainlink-resolved windows?

Runs backtest.barrier_eval over the paper ledger using the SAME Chainlink data
stream the markets resolve on for BOTH the window-open strike and the
window-close outcome (A1). Reports Brier(barrier_q) vs Brier(p), log-loss, and
calibration. This is the go/no-go for the whole strategy — if barrier_q does
NOT predict outcomes better than the market price, nothing downstream matters.

Requires CHAINLINK_API_KEY/SECRET (hard-fail closed). Run on the VPS:

    PYTHONPATH=. python3 scripts/barrier_eval_real.py --root data/paper
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.barrier_eval import BarrierEvalConfig, evaluate_barrier  # noqa: E402
from backtest.paper_ledger import load_trades  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data/paper")
    ap.add_argument("--out", default="reports/barrier_eval_real.txt")
    ap.add_argument(
        "--source", choices=("streams", "aggregator"), default="streams",
        help="streams = Chainlink Data Streams (needs creds, exact); "
        "aggregator = free on-chain AggregatorV3 (no creds, COARSE approx)",
    )
    args = ap.parse_args(argv)

    from connectors.chainlink import (
        oracle_agg_price_at,
        oracle_enabled,
        oracle_price_at,
    )

    caveat = ""
    if args.source == "streams":
        if not oracle_enabled():
            print("ABORT: CHAINLINK_API_KEY/SECRET not set. Either set them, or "
                  "run --source aggregator for a free COARSE preliminary read.")
            return 2

        def resolve(asset: str, ts: int) -> float:
            try:
                return float(oracle_price_at(asset, int(ts)) or 0.0)
            except Exception:  # noqa: BLE001
                return 0.0
        exclude_equal = False
    else:
        resolve = lambda asset, ts: float(oracle_agg_price_at(asset, int(ts)) or 0.0)
        exclude_equal = True
        caveat = (
            "\n*** SOURCE = on-chain AggregatorV3 (FREE, COARSE). Heartbeat/"
            "deviation updates mean many 15m windows fall in one round and are "
            "EXCLUDED (indeterminate). This APPROXIMATES the Data Streams feed "
            "Polymarket resolves on — a preliminary read, not the final go/no-go. ***"
        )

    ledgers = sorted(Path(args.root).glob("*/trade_ledger.jsonl"))
    trades = load_trades(ledgers)
    if not trades:
        print(f"no trades under {args.root}/*/trade_ledger.jsonl")
        return 2

    rep = evaluate_barrier(
        trades,
        open_price_fn=resolve,
        close_price_fn=resolve,       # same source, at window-close ts
        exclude_equal_close=exclude_equal,
        cfg=BarrierEvalConfig(),
    )
    text = rep.text()
    # Explicit go/no-go line
    if rep.barrier_brier is not None and rep.market_brier is not None:
        verdict = (
            "GO — barrier beats market" if rep.barrier_brier < rep.market_brier
            else "NO-GO — barrier does NOT beat the market price"
        )
        text += f"\n\n=== A3 VERDICT: {verdict} " \
                f"(barrier Brier {rep.barrier_brier:.4f} vs market {rep.market_brier:.4f}) ==="
    else:
        text += "\n\n=== A3 VERDICT: insufficient evaluable trades ==="
    text += caveat
    print(text)
    out = Path(args.out)
    out.parent.mkdir(exist_ok=True)
    out.write_text(text + "\n")
    print(f"\n[written → {out}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
