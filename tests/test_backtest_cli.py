"""CLI smoke tests for beginner-friendly backtest entrypoint."""

from __future__ import annotations

from backtest.cli import format_verdict, main


def test_format_verdict_pass():
    text = format_verdict(
        win_rate=0.827, n_trades=1312, max_dd=0.114, target_met=True, mc_p5=0.791
    )
    assert "Target met" in text
    assert "82.7%" in text
    assert "1,312" in text or "1312" in text
    assert "79.1%" in text
    assert "11.4%" in text


def test_cli_fast_exits_zero(monkeypatch, tmp_path):
    """python -m backtest --fast should hit ≥80% WR with defaults."""
    # Keep artifacts under tmp by monkeypatching ARTIFACT_ROOT
    import backtest.artifacts as art
    import backtest.cli as cli

    monkeypatch.setattr(art, "ARTIFACT_ROOT", tmp_path / "runs")
    monkeypatch.setattr(cli, "ARTIFACT_ROOT", tmp_path / "runs", raising=False)
    # Patch new_run_dir used inside cli via artifacts module
    from backtest import artifacts

    monkeypatch.setattr(
        artifacts,
        "ARTIFACT_ROOT",
        tmp_path / "runs",
    )
    code = main(["--fast", "--seed", "42", "--no-rich", "--filter-mode", "strict"])
    assert code == 0
    runs = list((tmp_path / "runs").glob("*"))
    assert runs, "expected timestamped artifact folder"
    report = runs[0] / "report.txt"
    assert report.is_file()
    body = report.read_text()
    assert "Reproduce with:" in body
    assert "--fast" in body
    assert (runs[0] / "parameters_used.yaml").is_file()
    assert (runs[0] / "report.json").is_file()
