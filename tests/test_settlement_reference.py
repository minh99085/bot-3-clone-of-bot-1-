"""Settlement correctness — resolve on close-vs-window-open, never fabricate.

Two hallucination bugs this pins:

  1. Wrong reference: the paper sim decided win/loss by comparing the exit
     CEX mid to the ENTRY CEX mid (mid-window), but Polymarket up/down
     markets resolve on close vs the window-OPEN strike. The reported PnL
     measured a different bet than the market would pay.

  2. Fabricated wins: when the CEX price was unavailable the sim did
     ``won = hash(signal_id) % 100 < 55`` — manufacturing a 55%-win-biased
     outcome from a hash. Pure invented PnL.

The fix resolves against the window-open reference and, when a reliable
open/exit price is unavailable, SKIPS the position (leaves it open) instead
of settling against the wrong reference or a hash.
"""

from __future__ import annotations

import time

import pytest

import hermes.settlement_fast as stl_mod
from hermes.state_io import append_jsonl


def _open_pos(ledger_file, *, slug, direction, entry_price=0.2, size=100.0, asset="BTC", mid=64000.0):
    append_jsonl(
        ledger_file,
        {
            "event": "position_open",
            "signal_id": f"sig_{direction}_{slug[-4:]}",
            "position_id": f"pos_{slug[-6:]}",
            "slug": slug,
            "direction": direction,
            "entry_price": entry_price,
            "size_usd": size,
            "opened_at": "2026-07-15T10:00:00Z",
            "meta": {"cex_mid": mid, "cex_asset": asset, "slug": slug},
        },
    )


def _setup(monkeypatch, tmp_path):
    paper = tmp_path / "paper" / "btc5"
    paper.mkdir(parents=True)
    ledger_file = paper / "trade_ledger.jsonl"
    refs = tmp_path / "settlement_refs.json"
    monkeypatch.setenv("HERMES_SETTLEMENT_REFS", str(refs))
    monkeypatch.setattr(stl_mod, "ledger_path", lambda paper=True, _p=ledger_file: _p)
    monkeypatch.setattr(stl_mod, "process_settlement", lambda _s: None)
    monkeypatch.setattr(stl_mod, "_polymarket_resolution", lambda slug: None)
    return ledger_file


def _slug(asset="btc", tf="5m"):
    old = int(time.time()) - 7200
    step = 300 if tf == "5m" else 900
    return f"{asset}-updown-{tf}-{old - (old % step)}"


def test_win_resolved_on_close_vs_open_not_entry(monkeypatch, tmp_path):
    """UP wins iff close > window-OPEN, regardless of the entry mid."""
    ledger = _setup(monkeypatch, tmp_path)
    slug = _slug()
    # Entry mid 64500, but window OPEN was 64000; close 64200 → up vs open.
    _open_pos(ledger, slug=slug, direction="UP", mid=64500.0)
    monkeypatch.setattr(stl_mod, "_open_price_at", lambda asset, ts: 64000.0)
    monkeypatch.setattr(stl_mod, "_close_price_at", lambda a, ts: 64200.0)
    out = stl_mod.settle_expired_paper_positions(paper=True)
    assert len(out) == 1
    # close (64200) > open (64000) → UP wins, even though close < entry (64500)
    assert out[0].won is True
    assert "open" in (out[0].notes or "").lower()


def test_down_wins_when_close_below_open(monkeypatch, tmp_path):
    ledger = _setup(monkeypatch, tmp_path)
    slug = _slug()
    _open_pos(ledger, slug=slug, direction="DOWN", mid=64500.0)
    monkeypatch.setattr(stl_mod, "_open_price_at", lambda asset, ts: 64000.0)
    monkeypatch.setattr(stl_mod, "_close_price_at", lambda a, ts: 63900.0)
    out = stl_mod.settle_expired_paper_positions(paper=True)
    assert len(out) == 1
    assert out[0].won is True  # close 63900 < open 64000 → DOWN wins


def test_no_open_reference_is_not_settled_no_fabrication(monkeypatch, tmp_path):
    """Unavailable open price → position NOT settled (never a hash-fabricated win)."""
    ledger = _setup(monkeypatch, tmp_path)
    slug = _slug()
    _open_pos(ledger, slug=slug, direction="UP", mid=64500.0)
    monkeypatch.setattr(stl_mod, "_open_price_at", lambda asset, ts: 0.0)  # unavailable
    monkeypatch.setattr(stl_mod, "_close_price_at", lambda a, ts: 64200.0)
    out = stl_mod.settle_expired_paper_positions(paper=True)
    assert out == []  # left open, not fabricated


def test_no_close_price_is_not_settled(monkeypatch, tmp_path):
    ledger = _setup(monkeypatch, tmp_path)
    slug = _slug()
    _open_pos(ledger, slug=slug, direction="DOWN", mid=64500.0)
    monkeypatch.setattr(stl_mod, "_open_price_at", lambda asset, ts: 64000.0)
    monkeypatch.setattr(stl_mod, "_close_price_at", lambda a, ts: 0.0)
    out = stl_mod.settle_expired_paper_positions(paper=True)
    assert out == []


def test_no_hash_fabrication_path_remains():
    """The 55%-win hash settlement must be gone from the source."""
    import inspect

    src = inspect.getsource(stl_mod)
    assert "% 100" not in src, "hash-based fake settlement still present"
    assert "hash(" not in src or "signal_id" not in src.split("hash(")[1][:40], (
        "hash(signal_id) fabrication still present"
    )
