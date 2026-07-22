"""Settlement exit must be the price AT window close, not the live mid minutes later.

Live evidence (btc-updown-15m-1784475900, report 2026-07-19 last3h): six lanes
settled the SAME window with the SAME side within 11 seconds and got
DIFFERENT outcomes — impossible for one binary market. The exit reference was
get_asset_mid() at each lane's settle moment (post-close drift), not the
window-close price Polymarket resolves on.
"""

from __future__ import annotations

import time

import hermes.settlement_fast as stl_mod
from hermes.state_io import append_jsonl


def _setup(monkeypatch, tmp_path):
    paper = tmp_path / "paper" / "lane01"
    paper.mkdir(parents=True)
    ledger = paper / "trade_ledger.jsonl"
    refs = tmp_path / "settlement_refs.json"
    monkeypatch.setenv("HERMES_SETTLEMENT_REFS", str(refs))
    monkeypatch.setattr(stl_mod, "ledger_path", lambda paper=True, _p=ledger: _p)
    monkeypatch.setattr(stl_mod, "process_settlement", lambda _s: None)
    monkeypatch.setattr(stl_mod, "_polymarket_resolution", lambda slug: None)
    return ledger


def _slug():
    old = int(time.time()) - 7200
    return f"btc-updown-15m-{old - (old % 900)}", old - (old % 900)


def _open(ledger, slug, direction="UP"):
    append_jsonl(ledger, {
        "event": "position_open", "signal_id": f"sig_{direction}",
        "position_id": "pos_1", "slug": slug, "direction": direction,
        "entry_price": 0.4, "size_usd": 60.0,
        "opened_at": "2026-07-15T10:00:00Z",
        "meta": {"cex_mid": 64000.0, "cex_asset": "BTC", "slug": slug},
    })


def test_exit_uses_close_price_not_live_mid(monkeypatch, tmp_path):
    ledger = _setup(monkeypatch, tmp_path)
    slug, wts = _slug()
    _open(ledger, slug, "UP")
    monkeypatch.setattr(stl_mod, "_open_price_at", lambda a, ts: 64000.0)
    # Close (at window end) was BELOW open → UP lost, even though the live
    # mid at settle time has since drifted above the strike.
    calls = {}

    def fake_close(asset, ts):
        calls["close_ts"] = ts
        return 63950.0

    monkeypatch.setattr(stl_mod, "_close_price_at", fake_close)
    monkeypatch.setattr(
        stl_mod, "get_asset_mid",
        lambda a, *, force_rest=False: (_ for _ in ()).throw(AssertionError(
            "settlement must not use the live mid as exit reference")),
    )
    out = stl_mod.settle_expired_paper_positions(paper=True)
    assert len(out) == 1
    assert out[0].won is False  # close vs open decides — not the drifted mid
    assert calls["close_ts"] == wts + 900  # exit sampled AT window end


def test_missing_close_price_skips_settlement(monkeypatch, tmp_path):
    ledger = _setup(monkeypatch, tmp_path)
    slug, _ = _slug()
    _open(ledger, slug, "DOWN")
    monkeypatch.setattr(stl_mod, "_open_price_at", lambda a, ts: 64000.0)
    monkeypatch.setattr(stl_mod, "_close_price_at", lambda a, ts: 0.0)
    out = stl_mod.settle_expired_paper_positions(paper=True)
    assert out == []  # left open — never guessed from the live mid
