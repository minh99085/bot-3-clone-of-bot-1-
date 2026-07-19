#!/usr/bin/env python3
"""Honest real-data report + barrier-vs-market evaluation from the paper ledger.

Run where the exported ledger lives (VPS or an allowlisted session), so the
barrier section can reach the CEX for window-open/klines:

    PYTHONPATH=. python3 scripts/real_report.py --ledger-root reports/paper_ledger_export
    PYTHONPATH=. python3 scripts/real_report.py --ledger-root data/paper --no-barrier

Reads every <root>/*/trade_ledger.jsonl (+ pretrade_decisions.jsonl for model
q), builds the after-cost report and the barrier eval, and writes the text
to reports/real_report_<label>.txt.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.paper_ledger import (  # noqa: E402
    default_pretrade_paths,
    full_report,
    load_trades,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger-root", default="reports/paper_ledger_export")
    ap.add_argument("--bankroll", type=float, default=2000.0)
    ap.add_argument("--no-barrier", dest="barrier", action="store_false", default=True)
    ap.add_argument("--label", default="latest")
    args = ap.parse_args(argv)

    root = Path(args.ledger_root)
    ledgers = sorted(root.glob("*/trade_ledger.jsonl"))
    pretrade = default_pretrade_paths(root)
    if not ledgers:
        print(f"no trade_ledger.jsonl under {root}/*/")
        return 2
    trades = load_trades(ledgers, pretrade_paths=pretrade)
    text = full_report(trades, bankroll=args.bankroll, run_barrier=args.barrier)
    print(text)
    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"real_report_{args.label}.txt"
    out_path.write_text(text + "\n")
    print(f"\n[written → {out_path}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
