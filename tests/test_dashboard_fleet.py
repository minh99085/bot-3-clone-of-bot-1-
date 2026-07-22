"""Fleet dashboard aggregation — 10 BTC15 lanes × $2k = $20k."""

from __future__ import annotations

import json

import hermes.dashboard_data as dashboard_data


def test_fleet_constants():
    assert dashboard_data.FLEET_INSTANCE_COUNT == 10
    assert dashboard_data.PER_INSTANCE_BANKROLL == 2000.0
    assert dashboard_data.FLEET_BANKROLL == 20000.0
    assert len(dashboard_data.INSTANCE_IDS) == 10
    assert dashboard_data.INSTANCE_IDS[0] == "lane01_baseline"
    assert dashboard_data.INSTANCE_IDS[1] == "lane02_autonomy"
    assert dashboard_data.INSTANCE_IDS[-1] == "lane10_depth"


def test_instance_cards_isolated(monkeypatch, tmp_path):
    paper = tmp_path / "paper"
    ids = dashboard_data.INSTANCE_IDS
    for iid in ids:
        d = paper / iid
        d.mkdir(parents=True)
        rows = [
            {
                "event": "fill",
                "signal_id": f"{iid}-1",
                "slug": "btc-updown-15m-1",
                "size_usd": 50,
                "fill_price": 0.9,
            },
            {
                "event": "settlement",
                "signal_id": f"{iid}-1",
                "slug": "btc-updown-15m-1",
                "pnl_usd": 10.0 if iid != "lane09_random" else -5.0,
                "won": iid != "lane09_random",
                "settled_at": "2026-07-15T10:00:00Z",
            },
        ]
        (d / "trade_ledger.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )

    monkeypatch.setattr(dashboard_data, "paper_dir", lambda: paper)

    cards = dashboard_data.instance_cards()
    assert len(cards) == 10
    by_id = {c["id"]: c for c in cards}

    assert by_id["lane01_baseline"]["bankroll"] == 2000.0
    assert by_id["lane01_baseline"]["equity"] == 2010.0
    assert by_id["lane01_baseline"]["trades"] == 1
    assert by_id["lane01_baseline"]["variant"] == "baseline"
    assert by_id["lane09_random"]["pnl"] == -5.0
    assert by_id["lane09_random"]["role"] == "null"

    fleet = dashboard_data.fleet_summary()
    assert fleet["fleet_bankroll"] == 20000.0
    # 9 winners (+10) + 1 loser (-5) = +85
    assert fleet["fleet_equity"] == 20085.0
    assert fleet["total_pnl"] == 85.0
    assert fleet["total_trades"] == 10
    assert fleet["wins"] == 9
    assert fleet["losses"] == 1


def test_fleet_equity_curve_chronological(monkeypatch, tmp_path):
    paper = tmp_path / "paper"
    for iid, pnl, ts in (
        ("lane01_baseline", 10.0, "2026-07-15T10:00:00Z"),
        ("lane02_autonomy", 20.0, "2026-07-15T10:01:00Z"),
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
    assert curve[0]["equity"] == 20000.0
    assert curve[-1]["equity"] == 20030.0
    assert curve[1]["ts"] == "2026-07-15T10:00:00Z"
    assert curve[2]["ts"] == "2026-07-15T10:01:00Z"


def test_load_state_fleet_bankroll():
    state = dashboard_data.load_state()
    assert state["fleet_bankroll_usd"] == 20000.0
    assert state["per_instance_bankroll_usd"] == 2000.0
    assert state["instance_count"] == 10


def test_lane_scoreboard(monkeypatch, tmp_path):
    paper = tmp_path / "paper"
    # Minimal paired ledgers: baseline beats random on same window
    for iid, pnl, won in (
        ("lane01_baseline", 8.0, True),
        ("lane09_random", -3.0, False),
    ):
        d = paper / iid
        d.mkdir(parents=True)
        rows = [
            {
                "event": "fill",
                "signal_id": f"{iid}-1",
                "slug": "btc-updown-15m-1700000000",
                "filled_at": "2026-07-15T10:00:00Z",
                "size_usd": 40,
                "fill_price": 0.55,
                "direction": "UP",
                "won": None,
            },
            {
                "event": "settlement",
                "signal_id": f"{iid}-1",
                "slug": "btc-updown-15m-1700000000",
                "settled_at": "2026-07-15T10:15:00Z",
                "pnl_usd": pnl,
                "won": won,
                "size_usd": 40,
                "entry_price": 0.55,
            },
        ]
        (d / "trade_ledger.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )

    monkeypatch.setattr(dashboard_data, "paper_dir", lambda: paper)
    board = dashboard_data.lane_scoreboard()
    assert board["null_lane"] == "lane09_random"
    by_lane = {r["lane"]: r for r in board["rows"]}
    assert "lane01_baseline" in by_lane
    assert by_lane["lane01_baseline"]["delta_vs_null"] == 11.0


def test_fleet_trade_history_newest_first(monkeypatch, tmp_path):
    paper = tmp_path / "paper"
    for iid, ts, pnl in (
        ("lane01_baseline", "2026-07-15T10:00:00Z", 10.0),
        ("lane03_favorite", "2026-07-15T11:00:00Z", -5.0),
        ("lane05_late", "2026-07-15T10:30:00Z", 8.0),
    ):
        d = paper / iid
        d.mkdir(parents=True)
        rows = [
            {
                "event": "settlement",
                "signal_id": f"{iid}-1",
                "slug": f"btc-updown-15m-{iid}",
                "settled_at": ts,
                "pnl_usd": pnl,
                "won": pnl > 0,
                "direction": "UP",
                "size_usd": 40,
            }
        ]
        (d / "trade_ledger.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )

    monkeypatch.setattr(dashboard_data, "paper_dir", lambda: paper)
    hist = dashboard_data.fleet_trade_history(50)
    assert len(hist) == 3
    assert hist[0]["instance_id"] == "lane03_favorite"
    assert hist[0]["lane"]
    assert hist[-1]["instance_id"] == "lane01_baseline"
