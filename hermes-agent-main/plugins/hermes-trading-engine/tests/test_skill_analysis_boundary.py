"""Tests for SKILL_ANALYSIS.md external cognitive boundary loader."""

from __future__ import annotations

from pathlib import Path

from engine.pulse.loop_architecture.skill_analysis_boundary import SkillAnalysisBoundary


_SAMPLE = """# SKILL_ANALYSIS.md — Bot External Cognitive Boundary

## 2. Operational Thresholds

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


def test_load_parses_threshold_table(tmp_path):
    skill_path = tmp_path / "SKILL_ANALYSIS.md"
    skill_path.write_text(_SAMPLE, encoding="utf-8")

    boundary = SkillAnalysisBoundary.load(tmp_path)

    assert boundary.loaded is True
    assert boundary.sweet_min == 0.48
    assert boundary.sweet_max == 0.54
    assert boundary.tail_max == 0.09
    assert boundary.min_depth_usd == 60.0
    assert boundary.max_slippage_pct == 1.5
    assert boundary.min_shares == 6.0
    assert boundary.tv_timeframes == ("15", "30")
    assert boundary.tail_min_strength == 0.60
    assert boundary.content_hash


def test_load_syncs_to_data_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL_ANALYSIS.md").write_text(_SAMPLE, encoding="utf-8")
    data = tmp_path / "data"

    boundary = SkillAnalysisBoundary.load(data)
    # default_paths checks data_dir first; seed via env path
    boundary = SkillAnalysisBoundary.load(src)
    dest = src / "SKILL_ANALYSIS.md"
    assert dest.exists()

    copied = data / "SKILL_ANALYSIS.md"
    boundary._sync_to_data_dir(data, dest.read_text(encoding="utf-8"))
    assert copied.exists()
    assert copied.read_text(encoding="utf-8") == _SAMPLE


def test_to_triage_config_uses_disk_thresholds(tmp_path, monkeypatch):
    monkeypatch.delenv("PULSE_TRIAGE_SWEET_MIN", raising=False)
    (tmp_path / "SKILL_ANALYSIS.md").write_text(_SAMPLE, encoding="utf-8")
    cfg = SkillAnalysisBoundary.load(tmp_path).to_triage_config()
    assert cfg.sweet_min == 0.48
    # MTF ladder comes from env (PULSE_TV_MTF_TIMEFRAMES), not disk SKILL_ANALYSIS.
    assert cfg.tv_timeframes == ("5", "15", "30", "60", "240", "1440")


def test_to_triage_config_prefers_env_when_sweet_min_set(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_TRIAGE_SWEET_MIN", "0.40")
    (tmp_path / "SKILL_ANALYSIS.md").write_text(_SAMPLE, encoding="utf-8")
    cfg = SkillAnalysisBoundary.load(tmp_path).to_triage_config()
    assert cfg.sweet_min == 0.40


def test_missing_file_uses_coded_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(
        SkillAnalysisBoundary,
        "default_paths",
        classmethod(lambda cls, data_dir=None: [tmp_path / "SKILL_ANALYSIS.md"]),
    )
    boundary = SkillAnalysisBoundary.load(tmp_path)
    assert boundary.loaded is False
    assert boundary.sweet_min == 0.47
    assert boundary.sweet_max == 0.55
