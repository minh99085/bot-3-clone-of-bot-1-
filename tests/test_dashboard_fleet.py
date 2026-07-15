"""Fleet dashboard aggregation — 5 instances × $2k = $10k."""

from __future__ import annotations

import json

import hermes.dashboard_data as dashboard_data


def test_fleet_constants():
    assert dashboard_data.FLEET_INSTANCE_COUNT == 5
    assert dashboard_data.PER_INSTANCE_BANKROLL == 2000.0
    assert dashboard_data.FLEET_BANKROLL == 10000.0


def test_instance_cards_isolated(monkeypatch, tmp_path):
    paper = tmp_path / "paper"
    for iid in ("btc5", "btc15", "eth5", "sol5", "rotator"):
        d = paper / iid
        d.mkdir(parents=True)
        rows = [
            {
                "event": "fill",
                "signal_id": f"{iid}-1",
                "slug": "btc-updown-5m-1",
                "size_usd": 50,
                "fill_price": 0.9,
            },
            {
                "event": "settlement",
                "signal_id": f"{iid}-1",
                "slug": "btc-updown-5m-1",
                "pnl_usd": 10.0 if iid != "sol5" else -5.0,
                "won": iid != "sol5",
                "settled_at": f"2026-07-15T10:00:00Z",
            },
        ]
        (d / "trade_ledger.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )

    monkeypatch.setattr(dashboard_data, "paper_dir", lambda: paper)

    cards = dashboard_data.instance_cards()
    assert len(cards) == 5
    by_id = {c["id"]: c for c in cards}

    assert by_id["btc5"]["bankroll"] == 2000.0
    assert by_id["btc5"]["equity"] == 2010.0
    assert by_id["btc5"]["trades"] == 1
    assert by_id["sol5"]["pnl"] == -5.0

    fleet = dashboard_data.fleet_summary()
    assert fleet["fleet_bankroll"] == 10000.0
    # 4 winners (+10) + 1 loser (-5) = +35
    assert fleet["fleet_equity"] == 10035.0
    assert fleet["total_pnl"] == 35.0
    assert fleet["total_trades"] == 5
    assert fleet["wins"] == 4
    assert fleet["losses"] == 1


def test_fleet_equity_curve_chronological(monkeypatch, tmp_path):
    paper = tmp_path / "paper"
    for iid, pnl, ts in (
        ("btc5", 10.0, "2026-07-15T10:00:00Z"),
        ("btc15", 20.0, "2026-07-15T10:01:00Z"),
    ):
        d = paper / iid
        d.mkdir(parents=True)
        rows = [
            {
                "event": "settlement",
                "signal_id": f"{iid}-1",
                "pnl_usd": pnl,
                "won": True,
                "settled_at": ts,
            }
        ]
        (d / "trade_ledger.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )

    monkeypatch.setattr(dashboard_data, "paper_dir", lambda: paper)
    curve = dashboard_data.fleet_equity_curve()
    assert curve[0]["equity"] == 10000.0
    assert curve[-1]["equity"] == 10030.0
    assert curve[1]["ts"] == "2026-07-15T10:00:00Z"
    assert curve[2]["ts"] == "2026-07-15T10:01:00Z"


def test_load_state_fleet_bankroll():
    state = dashboard_data.load_state()
    assert state["fleet_bankroll_usd"] == 10000.0
    assert state["per_instance_bankroll_usd"] == 2000.0
    assert state["instance_count"] == 5
