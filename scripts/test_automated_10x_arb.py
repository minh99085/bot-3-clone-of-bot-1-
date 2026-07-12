"""Tests for automated_10x_arb cloud loop + skill_analysis_loader."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from skill_analysis_loader import SkillThresholds  # noqa: E402

_SAMPLE_SKILL = """# SKILL_ANALYSIS.md
| Key | Value |
|-----|-------|
| `sweet_min` | 0.48 |
| `sweet_max` | 0.54 |
| `tail_max` | 0.09 |
| `min_depth_usd` | 60 |
| `max_slippage_pct` | 1.5 |
| `min_shares` | 6 |
| `tv_timeframes` | 15, 30 |
| `tail_min_strength` | 0.60 |
"""


def test_skill_loader_parses_table(tmp_path):
    (tmp_path / "SKILL_ANALYSIS.md").write_text(_SAMPLE_SKILL, encoding="utf-8")
    skill = SkillThresholds.load(tmp_path)
    assert skill.loaded is True
    assert skill.sweet_min == 0.48
    assert skill.tv_timeframes == ("15", "30")


def test_classify_ask():
    skill = SkillThresholds(path=Path("x"))
    skill.sweet_min, skill.sweet_max, skill.tail_max = 0.47, 0.55, 0.10
    assert skill.classify_ask(0.50) == "PROCEED_SWEEP"
    assert skill.classify_ask(0.08, tail_breakthrough=True) == "PROCEED_10X"
    assert skill.classify_ask(0.08, tail_breakthrough=False) == "REJECT_NO_BREAKTHROUGH"
    assert skill.classify_ask(0.70) == "REJECT_PRICE_OUT_OF_BAND"


def test_write_memory_explicit_sections(tmp_path, monkeypatch):
    import automated_10x_arb as mod

    mem = tmp_path / "MEMORY.md"
    logs = tmp_path / "LOGS.txt"
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "MEMORY_PATH", mem)
    monkeypatch.setattr(mod, "LOGS_PATH", logs)

    skill = SkillThresholds.load(ROOT)
    mod.write_memory(
        skill=skill,
        wallet={"on_hand_capital_usd": 516.52, "open_positions_count": 1},
        open_trades=[{
            "event_id": "evt-1",
            "side": "up",
            "entry_price": 0.50,
            "size_usd": 5.0,
            "token_id": "tok-abc",
            "time_boundary": "1782369999",
            "status": "open",
        }],
        candidates=[],
        run_id="test-run",
    )
    text = mem.read_text(encoding="utf-8")
    assert "## Wallet balances (paper)" in text
    assert "## Open trades" in text
    assert "evt-1" in text
    assert "516.52" in text
    assert "## Skill thresholds (SKILL_ANALYSIS.md)" in text
    assert "Loop Engineering architecture LOCKED" in text


def test_run_cycle_with_mocked_vps(tmp_path, monkeypatch):
    import automated_10x_arb as mod

    mem = tmp_path / "MEMORY.md"
    logs = tmp_path / "LOGS.txt"
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "MEMORY_PATH", mem)
    monkeypatch.setattr(mod, "LOGS_PATH", logs)

    status = {"capital": {"on_hand_capital_usd": 500.0, "open_positions": 0}, "ts": 1.0, "ticks": 10}
    ledger = {
        "stats": {"open_positions": 0},
        "positions": [{
            "event_id": "e2",
            "side": "down",
            "entry_price": 0.52,
            "size_usd": 5.0,
            "token_id": "dn-tok",
            "close_ts": "999",
            "status": "open",
        }],
    }

    with patch.object(mod, "pull_vps_state", return_value=(status, ledger)), \
         patch.object(mod, "discover_crypto_candidates", return_value=[]):
        rc = mod.run_cycle(vps_url="http://test", discovery=False)

    assert rc == 0
    assert mem.exists()
    body = mem.read_text(encoding="utf-8")
    assert "e2" in body
    assert "down" in body
