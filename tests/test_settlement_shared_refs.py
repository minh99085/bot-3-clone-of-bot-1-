"""Fleet settlement must agree across lanes on the same window."""

from __future__ import annotations

import json
import time

import hermes.settlement_fast as stl_mod
from hermes.state_io import append_jsonl


def _slug():
    old = int(time.time()) - 7200
    wts = old - (old % 900)
    return f"btc-updown-15m-{wts}", wts


def _setup_lane(monkeypatch, tmp_path, lane: str):
    paper = tmp_path / "paper" / lane
    paper.mkdir(parents=True)
    ledger = paper / "trade_ledger.jsonl"
    refs = tmp_path / "shared_refs.json"
    monkeypatch.setenv("HERMES_SETTLEMENT_REFS", str(refs))
    monkeypatch.setattr(stl_mod, "ledger_path", lambda paper=True, _p=ledger: _p)
    monkeypatch.setattr(stl_mod, "process_settlement", lambda _s: None)
    monkeypatch.setattr(stl_mod, "_polymarket_resolution", lambda slug: None)
    return ledger, refs


def _open(ledger, slug, direction="UP", signal_id="sig_a", strike=99999.0):
    # Deliberately poison meta.strike — settlement must ignore it.
    append_jsonl(
        ledger,
        {
            "event": "position_open",
            "signal_id": signal_id,
            "position_id": signal_id,
            "slug": slug,
            "direction": direction,
            "entry_price": 0.4,
            "size_usd": 40.0,
            "opened_at": "2026-07-15T10:00:00Z",
            "meta": {
                "cex_mid": 64000.0,
                "cex_asset": "BTC",
                "slug": slug,
                "strike": strike,
                "price_to_beat": strike,
            },
        },
    )


def test_ignores_per_lane_strike_uses_window_open(monkeypatch, tmp_path):
    ledger, _ = _setup_lane(monkeypatch, tmp_path, "lane_a")
    slug, _ = _slug()
    # Poisoned strike ABOVE close would flip UP→loss if settlement used it.
    _open(ledger, slug, "UP", strike=64200.0)
    monkeypatch.setattr(stl_mod, "_open_price_at", lambda a, ts: 64000.0)
    monkeypatch.setattr(stl_mod, "_close_price_at", lambda a, ts: 64100.0)
    out = stl_mod.settle_expired_paper_positions(paper=True)
    assert len(out) == 1
    # close 64100 >= window-open 64000 → UP wins (strike 64200 must be ignored)
    assert out[0].won is True
    assert "open_cex=64000" in (out[0].notes or "")


def test_second_lane_reuses_shared_cache(monkeypatch, tmp_path):
    slug, _ = _slug()
    ledger_a, refs = _setup_lane(monkeypatch, tmp_path, "lane_a")
    _open(ledger_a, slug, "UP", signal_id="sig_a")
    monkeypatch.setattr(stl_mod, "_open_price_at", lambda a, ts: 64000.0)
    monkeypatch.setattr(stl_mod, "_close_price_at", lambda a, ts: 64100.0)
    out_a = stl_mod.settle_expired_paper_positions(paper=True)
    assert len(out_a) == 1 and out_a[0].won is True
    assert refs.is_file()

    # Lane B: CEX lookup would flip the outcome — shared cache must win.
    ledger_b, _ = _setup_lane(monkeypatch, tmp_path, "lane_b")
    _open(ledger_b, slug, "UP", signal_id="sig_b")
    monkeypatch.setattr(stl_mod, "_open_price_at", lambda a, ts: 64000.0)
    monkeypatch.setattr(stl_mod, "_close_price_at", lambda a, ts: 63900.0)
    out_b = stl_mod.settle_expired_paper_positions(paper=True)
    assert len(out_b) == 1
    assert out_b[0].won is True  # same as lane A via shared cache
    assert "shared_cache" in (out_b[0].notes or "")


def test_polymarket_resolution_preferred(monkeypatch, tmp_path):
    ledger, refs = _setup_lane(monkeypatch, tmp_path, "lane_a")
    slug, _ = _slug()
    _open(ledger, slug, "DOWN", signal_id="sig_a")
    monkeypatch.setattr(stl_mod, "_polymarket_resolution", lambda s: True)  # market UP
    monkeypatch.setattr(
        stl_mod,
        "_open_price_at",
        lambda a, ts: (_ for _ in ()).throw(AssertionError("cex unused")),
    )
    out = stl_mod.settle_expired_paper_positions(paper=True)
    assert len(out) == 1
    assert out[0].won is False  # DOWN loses when market UP
    assert "polymarket" in (out[0].notes or "")
    cached = json.loads(refs.read_text())
    assert cached[slug]["moved_up"] is True
