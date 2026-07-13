"""Tests for offline Polymarket replay enrichment."""

from __future__ import annotations

import json
from pathlib import Path

from engine.pulse.offline_replay import (
    binary_pnl,
    build_positions,
    evaluate_holdout,
    favorite_filter,
    parse_window_event,
    price_at,
    run_pipeline,
    summarize_cohort,
    train_learners,
    walk_forward_split,
    winner_from_market,
)


def test_winner_and_pnl():
    assert winner_from_market({"outcomes": ["Up", "Down"], "outcomePrices": ["0", "1"]}) == ("down", False)
    assert binary_pnl(True, 0.50, 5.0) == 5.0
    assert binary_pnl(False, 0.50, 5.0) == -5.0


def test_price_at_nearest():
    hist = [(100.0, 0.40), (200.0, 0.60), (300.0, 0.80)]
    assert price_at(hist, 210.0) == 0.60


def test_parse_window_and_pipeline(tmp_path: Path):
    spec = {"asset": "btc", "lane": "15m", "series_slug": "btc-up-or-down-15m", "window_seconds": 900}
    open_ts = 1_000_000.0
    close_ts = open_ts + 900
    up_tok = "up-token-1"
    down_tok = "down-token-1"
    ev = {
        "id": "1",
        "slug": "btc-updown-15m-1000000",
        "markets": [{
            "id": "m1",
            "conditionId": "0xabc",
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["1", "0"]',
            "clobTokenIds": json.dumps([up_tok, down_tok]),
            "endDate": "2026-01-01T00:15:00Z",
            "startDate": "2026-01-01T00:00:00Z",
        }],
        "endDate": "2026-01-01T00:15:00Z",
    }
    # Override with unix-aligned times via endDate that matches close_ts
    from datetime import datetime, timezone
    ev["markets"][0]["endDate"] = datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev["markets"][0]["startDate"] = datetime.fromtimestamp(open_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    w = parse_window_event(ev, spec)
    assert w is not None
    assert w.up_won is True

    root = tmp_path / "data"
    gamma = root / "raw" / "gamma" / "btc-up-or-down-15m"
    prices = root / "raw" / "clob" / "prices"
    gamma.mkdir(parents=True)
    prices.mkdir(parents=True)
    (gamma / "btc-updown-15m-1000000.json").write_text(json.dumps(ev), encoding="utf-8")
    mid = open_ts + 450
    (prices / ("%s.json" % up_tok)).write_text(
        json.dumps([{"t": mid, "p": 0.55}, {"t": mid + 60, "p": 0.60}]), encoding="utf-8")
    (prices / ("%s.json" % down_tok)).write_text(
        json.dumps([{"t": mid, "p": 0.45}, {"t": mid + 60, "p": 0.40}]), encoding="utf-8")

    # Second window for walk-forward
    open2, close2 = open_ts + 900, close_ts + 900
    ev2 = json.loads(json.dumps(ev))
    ev2["slug"] = "btc-updown-15m-1000900"
    ev2["markets"][0]["endDate"] = datetime.fromtimestamp(close2, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev2["markets"][0]["startDate"] = datetime.fromtimestamp(open2, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev2["markets"][0]["outcomePrices"] = '["0", "1"]'  # down wins
    up2, down2 = "up-token-2", "down-token-2"
    ev2["markets"][0]["clobTokenIds"] = json.dumps([up2, down2])
    (gamma / "btc-updown-15m-1000900.json").write_text(json.dumps(ev2), encoding="utf-8")
    mid2 = open2 + 450
    (prices / ("%s.json" % up2)).write_text(json.dumps([{"t": mid2, "p": 0.52}]), encoding="utf-8")
    (prices / ("%s.json" % down2)).write_text(json.dumps([{"t": mid2, "p": 0.48}]), encoding="utf-8")

    positions = build_positions(root, entry_modes=("mid",), both_sides=True)
    assert len(positions) >= 4
    assert all(p["status"] == "settled" for p in positions)
    assert all(0.05 < p["entry_price"] < 0.95 for p in positions)

    train, hold = walk_forward_split(positions, holdout_fraction=0.5)
    assert len(train) + len(hold) == len(positions)

    state = train_learners(train, data_dir=tmp_path / "replay")
    assert "lane_15m_learner" in state
    assert "cell_learning" in state

    fav = favorite_filter(positions, 0.48)
    assert summarize_cohort(fav)["n"] == len(fav)

    report = run_pipeline(root, out_dir=tmp_path / "out", entry_modes=("mid",), holdout_fraction=0.5)
    assert report["n_positions"] >= 4
    assert (tmp_path / "out" / "enriched_ledger.json").exists()
    assert (tmp_path / "out" / "walk_forward_report.json").exists()
    assert (tmp_path / "out" / "lane_15m_learner.json").exists()
    assert evaluate_holdout(hold)["all"]["n"] == len(hold)
