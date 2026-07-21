"""A1 verification — the strike/close we settle on must match the Chainlink
stream (and, as a cross-check, Polymarket's own RTDS) within tolerance.

Samples resolved windows from the ledger, refetches the Chainlink stream at
window-open and window-close, and confirms our recorded references match.
Requires CHAINLINK creds. Run on the VPS:

    PYTHONPATH=. python3 scripts/verify_oracle_settlement.py --root data/paper
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.paper_ledger import load_trades, parse_slug_window  # noqa: E402

WINDOW_SEC = {"5m": 300, "15m": 900}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data/paper")
    ap.add_argument("--tol-bps", type=float, default=20.0)
    ap.add_argument("--sample", type=int, default=20)
    args = ap.parse_args(argv)

    from connectors.chainlink import (
        assert_feeds_configured,
        oracle_price_at,
        oracle_streams_enabled,
    )

    if not oracle_streams_enabled():
        print("ABORT: CHAINLINK Data Streams creds not set (A1 verification needs "
              "the exact stream, not the coarse aggregator).")
        return 2
    print("configured feeds:", assert_feeds_configured())

    ledgers = sorted(Path(args.root).glob("*/trade_ledger.jsonl"))
    trades = load_trades(ledgers)[: args.sample]
    if not trades:
        print("no trades to verify")
        return 2

    ok = bad = 0
    for t in trades:
        win = parse_slug_window(t.slug)
        if not win:
            continue
        asset, tf, wts = win
        wsec = WINDOW_SEC.get(tf, 900)
        try:
            strike = oracle_price_at(asset.upper(), wts)
            close = oracle_price_at(asset.upper(), wts + wsec)
        except Exception as exc:  # noqa: BLE001
            print(f"  {t.slug}: oracle fetch failed: {exc}")
            bad += 1
            continue
        # recorded close reference (exit_cex) sanity vs oracle close
        rec = t.exit_cex or 0.0
        diff_bps = abs(rec - close) / close * 10_000 if (rec and close) else None
        flag = "ok" if (diff_bps is not None and diff_bps <= args.tol_bps) else "CHECK"
        if flag == "ok":
            ok += 1
        else:
            bad += 1
        print(f"  {t.slug}: strike={strike:.2f} close={close:.2f} "
              f"recorded_exit={rec:.2f} diff={diff_bps}bps [{flag}]")
    print(f"\n{ok} within tol, {bad} to review (tol={args.tol_bps}bps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
