"""Report epoch scoping for published artifacts."""

from __future__ import annotations

from engine.pulse.report_epoch import filter_ledger_doc, make_epoch


def test_filter_ledger_excludes_pre_epoch_positions():
    epoch = make_epoch(ts=1000.0, token="t1")
    ledger = {
        "positions": {
            "old": {"status": "settled", "entry_ts": 900.0, "pnl_usd": -5.0, "won": False},
            "new": {"status": "settled", "entry_ts": 1100.0, "pnl_usd": 3.0, "won": True},
        },
        "stats": {"trades": 2, "settled": 2, "realized_pnl_usd": -2.0},
        "accounting_state": {
            "arb_ledger": {
                "positions": {
                    "a1": {"status": "settled", "entry_ts": 800.0, "realized_profit_usd": 1.0},
                    "a2": {"status": "settled", "entry_ts": 1200.0, "realized_profit_usd": 2.0},
                },
                "executed": 2,
                "realized_profit_usd": 3.0,
            }
        },
    }
    out = filter_ledger_doc(ledger, epoch)
    assert len(out["positions"]) == 1
    assert out["stats"]["settled"] == 1
    assert out["stats"]["realized_pnl_usd"] == 3.0
    arb = out["accounting_state"]["arb_ledger"]
    assert arb["executed"] == 1
    assert arb["realized_profit_usd"] == 2.0
    assert out["report_epoch"]["token"] == "t1"
