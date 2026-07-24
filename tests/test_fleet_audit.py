"""Forensic audit: lifetime count is monotonic across renames (no fading);
recomputed PnL catches fabrication (no dreaming)."""

from __future__ import annotations

import json

import pytest

import hermes.dashboard_data as dd
from hermes.settlement_fast import settlement_pnl_usd


def _settle(sid, won, size, entry):
    return {
        "event": "settlement", "signal_id": sid, "slug": "btc-updown-15m-1",
        "won": won, "size_usd": size, "entry_price": entry,
        "pnl_usd": settlement_pnl_usd(won=won, size_usd=size, entry_price=entry),
        "settled_at": "2026-07-24T00:00:00Z",
    }


def _write(root, lane, rows):
    d = root / lane
    d.mkdir(parents=True, exist_ok=True)
    (d / "trade_ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    root = tmp_path / "paper"
    root.mkdir()
    monkeypatch.setattr(dd, "paper_dir", lambda: root)
    # one ACTIVE lane and one ORPHANED (renamed-away) lane
    monkeypatch.setattr(dd, "INSTANCE_IDS", ("lane10_favopen",))
    _write(root, "lane10_favopen", [
        _settle("a1", True, 40, 0.80), _settle("a2", False, 40, 0.80),
    ])
    _write(root, "lane06_garch", [  # orphaned: renamed to lane06_favlearn
        _settle("g1", True, 40, 0.50), _settle("g2", True, 40, 0.50),
        _settle("g3", False, 40, 0.50),
    ])
    return root


def test_lifetime_is_monotonic_includes_orphans(fleet):
    stats = dd.fleet_lifetime_stats()
    assert stats["lifetime_settled"] == 5          # 2 active + 3 orphaned
    assert stats["orphaned_settled"] == 3
    assert stats["orphaned_lanes"] == ["lane06_garch"]
    # active-only view (what the dashboard shows) is SMALLER — the "fade"
    assert len(dd.load_trades()) == 2 * 1  # only lane10 rows, per instance_paper_dirs


def test_lifetime_pnl_equals_sum_of_all_lanes(fleet):
    stats = dd.fleet_lifetime_stats()
    # active: +10 (win@0.80) -40 = -30 ; orphan: +40+40-40 = +40 ; total +10
    assert stats["lifetime_pnl"] == pytest.approx(
        settlement_pnl_usd(won=True, size_usd=40, entry_price=0.80) - 40
        + 40 + 40 - 40
    )


def test_audit_script_clean_on_consistent_ledgers(fleet, monkeypatch):
    import scripts.audit_fleet as af

    monkeypatch.setattr(af, "INSTANCE_IDS", ("lane10_favopen",))
    rc = af.main(["--root", str(fleet)])
    assert rc == 0  # all PnL reconciles → CLEAN


def test_audit_script_catches_fabricated_pnl(fleet, monkeypatch):
    import scripts.audit_fleet as af

    # DREAMING: tamper a stored PnL so it no longer matches the formula
    led = fleet / "lane10_favopen" / "trade_ledger.jsonl"
    rows = [json.loads(x) for x in led.read_text().splitlines()]
    rows[0]["pnl_usd"] = 999.99  # fabricated win
    led.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    monkeypatch.setattr(af, "INSTANCE_IDS", ("lane10_favopen",))
    rc = af.main(["--root", str(fleet)])
    assert rc == 1  # formula mismatch → INTEGRITY FAILURE


def test_audit_script_catches_duplicate_signal_ids(fleet, monkeypatch):
    import scripts.audit_fleet as af

    _write(fleet, "lane10_favopen", [
        _settle("dup", True, 40, 0.80), _settle("dup", True, 40, 0.80),
    ])
    monkeypatch.setattr(af, "INSTANCE_IDS", ("lane10_favopen",))
    assert af.main(["--root", str(fleet)]) == 1  # double-counted → FAIL
