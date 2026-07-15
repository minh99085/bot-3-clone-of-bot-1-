"""Paper desk guardrails — window expiry, extreme prices, settlement sanity."""

from __future__ import annotations

import time

import pytest

from hermes.market_scope import (
    EXTREME_PRICE_HIGH,
    EXTREME_PRICE_LOW,
    MIN_WINDOW_REMAINING_SEC,
    candidate_slugs_for_filter,
    is_extreme_entry_price,
    is_extreme_market_price,
    is_window_expired,
    is_window_tradeable,
    window_remaining_seconds,
)


def test_window_tradeable_requires_future_end():
    step = 300
    now = 1_700_000_000.0
    base = int(now // step) * step
    # Window ending in 90s — tradeable
    slug_ok = f"btc-updown-5m-{base}"
    assert window_remaining_seconds(slug_ok, now=now) == pytest.approx(300 - (now - base), abs=1)
    assert is_window_tradeable(slug_ok, now=now) is True
    assert is_window_expired(slug_ok, now=now) is False

    # Window that ended 2 minutes ago — not tradeable, expired
    slug_old = f"btc-updown-5m-{base - step}"
    assert is_window_tradeable(slug_old, now=now) is False
    assert is_window_expired(slug_old, now=now) is True


def test_extreme_price_blocks_penny_entries():
    assert is_extreme_market_price(0.01) is True
    assert is_extreme_market_price(0.99) is True
    assert is_extreme_market_price(0.50) is False
    assert is_extreme_entry_price(0.50, "UP") is False
    assert is_extreme_entry_price(0.03, "UP") is True
    assert is_extreme_entry_price(0.01, "UP") is True
    assert is_extreme_entry_price(0.925, "DOWN") is True
    assert is_extreme_entry_price(0.99, "DOWN") is True


def test_candidate_slugs_exclude_expired_offsets(monkeypatch):
    monkeypatch.setenv("MARKET_FILTER", "btc5")
    step = 300
    now = time.time()
    base = int(now // step) * step
    slugs = candidate_slugs_for_filter("btc5", now=now)
    assert slugs
    for slug in slugs:
        assert is_window_tradeable(slug, now=now)
    # Must not include previous window
    stale = f"btc-updown-5m-{base - step}"
    assert stale not in slugs


def test_settlement_uses_asset_cex_and_caps_pnl(monkeypatch, tmp_path):
    import hermes.settlement_fast as stl_mod
    from hermes.state_io import append_jsonl, ledger_path

    paper = tmp_path / "paper" / "eth5"
    paper.mkdir(parents=True)
    ledger_file = paper / "trade_ledger.jsonl"
    monkeypatch.setattr(stl_mod, "ledger_path", lambda paper=True, _p=ledger_file: _p)
    monkeypatch.setattr(stl_mod, "process_settlement", lambda _s: None)

    # Window ended long ago
    old_ts = int(time.time()) - 7200
    slug = f"eth-updown-5m-{old_ts - (old_ts % 300)}"

    append_jsonl(
        ledger_file,
        {
            "event": "position_open",
            "signal_id": "sig_test",
            "position_id": "pos_test",
            "slug": slug,
            "direction": "UP",
            "entry_price": 0.01,
            "size_usd": 60.0,
            "opened_at": "2026-07-15T10:00:00Z",
            "meta": {
                "cex_mid": 3400.0,
                "cex_asset": "ETH",
                "slug": slug,
            },
        },
    )

    calls: list[str] = []

    def fake_mid(asset: str, *, force_rest: bool = False) -> float:
        calls.append(asset)
        return 3410.0 if asset == "ETH" else 65000.0

    monkeypatch.setattr(stl_mod, "get_asset_mid", fake_mid)

    out = stl_mod.settle_expired_paper_positions(paper=True)
    assert len(out) == 1
    assert calls == ["ETH"]
    # Win capped: 60 * 5 = 300 max (not 5940)
    assert out[0].won is True
    assert out[0].pnl_usd == 300.0
    assert "asset=ETH" in (out[0].notes or "")


def test_settlement_rejects_btc_cex_on_sol_slug(monkeypatch, tmp_path):
    import hermes.settlement_fast as stl_mod
    from hermes.state_io import append_jsonl

    paper = tmp_path / "paper" / "sol5"
    paper.mkdir(parents=True)
    ledger_file = paper / "trade_ledger.jsonl"
    monkeypatch.setattr(stl_mod, "ledger_path", lambda paper=True, _p=ledger_file: _p)
    monkeypatch.setattr(stl_mod, "process_settlement", lambda _s: None)

    old_ts = int(time.time()) - 7200
    slug = f"sol-updown-5m-{old_ts - (old_ts % 300)}"

    append_jsonl(
        ledger_file,
        {
            "event": "position_open",
            "signal_id": "sig_sol",
            "position_id": "pos_sol",
            "slug": slug,
            "direction": "DOWN",
            "entry_price": 0.35,
            "size_usd": 60.0,
            "opened_at": "2026-07-15T10:00:00Z",
            "meta": {
                "cex_mid": 64872.95,
                "cex_asset": "BTC",
                "slug": slug,
            },
        },
    )

    calls: list[str] = []

    def fake_mid(asset: str, *, force_rest: bool = False) -> float:
        calls.append(asset)
        return 148.5 if asset == "SOL" else 65000.0

    monkeypatch.setattr(stl_mod, "get_asset_mid", fake_mid)

    out = stl_mod.settle_expired_paper_positions(paper=True)
    assert len(out) == 1
    assert calls == ["SOL"]
    assert "asset=SOL" in (out[0].notes or "")


def test_verifier_rejects_expired_window_and_extreme_price(monkeypatch):
    from hermes.models import Direction, Signal, EntryMode, Regime, ConfidenceTier
    from hermes.verifier import verify_signal

    monkeypatch.setenv("HERMES_SCOPE_BTC_UPDOWN_ONLY", "1")
    step = 300
    now = time.time()
    base = int(now // step) * step
    expired_slug = f"btc-updown-5m-{base - step}"

    sig = Signal(
        market_id="m1",
        slug=expired_slug,
        question="BTC up/down test",
        direction=Direction.UP,
        entry_mode=EntryMode.MISPRICING,
        confidence_tier=ConfidenceTier.A,
        conviction=0.95,
        fair_value=0.9,
        market_price=0.01,
        expected_edge=0.5,
        live_ev=0.4,
        regime=Regime.LOW_VOL,
        hourly_bucket=12,
        timeframe="5m",
        meta={
            "enhanced_misprice": True,
            "enhanced_passes": True,
            "yes_price": 0.01,
            "paper": True,
        },
    )
    report = verify_signal(sig, proposal=None, state={}, buckets=[])
    assert report.decision.value == "REJECT"
    assert any("window" in r or "extreme" in r for r in report.rejection_reasons)
