"""Tests for offline prior import merging into live data dir / ledger."""

from __future__ import annotations

import json
from pathlib import Path

from importlib.util import module_from_spec, spec_from_file_location


def _load_import_mod():
    path = Path(__file__).resolve().parents[4] / "scripts" / "polymarket-backfill" / "import_learner_priors.py"
    # workspace layout: hermes-agent-main/plugins/hermes-trading-engine/tests -> parents[3]=hermes-agent-main, need repo root
    # tests is at .../hermes-trading-engine/tests -> parents[0]=tests, [1]=hte, [2]=plugins, [3]=hermes-agent-main, [4]=repo
    spec = spec_from_file_location("import_learner_priors", path)
    mod = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_merge_cells_prefers_richer_offline(tmp_path: Path):
    mod = _load_import_mod()
    live = {"cells": {"a": {"evals": 1, "trades": 2, "wins": 1, "pnl_usd": 1.0}}}
    offline = {"cells": {"a": {"evals": 10, "trades": 20, "wins": 15, "pnl_usd": 5.0},
                         "b": {"evals": 3, "trades": 3, "wins": 0, "pnl_usd": -3.0}}}
    merged = mod._merge_cells(live, offline)
    assert merged["cells"]["a"]["trades"] == 20
    assert merged["cells"]["b"]["trades"] == 3


def test_import_patches_ledger(tmp_path: Path):
    mod = _load_import_mod()
    replay = tmp_path / "replay"
    data = tmp_path / "data"
    replay.mkdir()
    data.mkdir()
    (replay / "directional_cell_learning.json").write_text(json.dumps({
        "schema": "directional_cell_learning/2.0",
        "cells": {"btc|15m|up|0-5m|unknown|∅|sweet": {
            "evals": 12, "trades": 12, "wins": 9, "pnl_usd": 4.0}},
    }), encoding="utf-8")
    (replay / "lane_15m_learner.json").write_text(json.dumps({
        "policy": {"min_entry_price": 0.58, "sweet_min": 0.58, "sweet_max": 0.78},
        "recent": [],
    }), encoding="utf-8")
    (replay / "walk_forward_report.json").write_text(json.dumps({"holdout": {}}), encoding="utf-8")
    (data / "btc_pulse_ledger.json").write_text(json.dumps({
        "positions": {},
        "accounting_state": {
            "cell_learning": {"cells": {}},
            "lane_15m_learner": {"policy": {"min_entry_price": 0.0}},
        },
    }), encoding="utf-8")

    man = mod.import_priors(replay_dir=replay, data_dir=data, dry_run=False)
    assert man["actions"]
    ledger = json.loads((data / "btc_pulse_ledger.json").read_text(encoding="utf-8"))
    cells = ledger["accounting_state"]["cell_learning"]["cells"]
    assert len(cells) == 1
    assert cells["btc|15m|up|0-5m|unknown|∅|sweet"]["trades"] == 12
    assert ledger["accounting_state"]["lane_15m_learner"]["policy"]["min_entry_price"] == 0.58
    assert (data / "lane_15m_learner_offline_prior.json").exists()
