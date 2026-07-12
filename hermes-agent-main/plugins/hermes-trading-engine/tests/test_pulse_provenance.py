"""Provenance artifact generation tests."""

from __future__ import annotations

from engine.pulse.provenance import (
    build_manifest,
    build_validation_full,
    build_validation_light,
    write_provenance_artifacts,
)


def test_provenance_artifacts_written(tmp_path):
    light = {
        "global_reconciled": True,
        "live_trading_enabled": False,
        "ledger": {"settled": 3},
        "reconciliation": {"failed_checks": []},
        "execution_realistic_edge": {"payoff_guards": {"rejected_tiny_upside": 1}},
    }
    written = write_provenance_artifacts(
        tmp_path, light_report=light, status={"ticks": 10}, ledger={"stats": {"settled": 3}})
    assert set(written) == {"MANIFEST.txt", "validation_light.txt", "validation_full.txt"}
    assert (tmp_path / "MANIFEST.txt").exists()
    manifest = (tmp_path / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "paper_only=true" in manifest
    assert "MANIFEST.txt" in manifest


def test_validation_light_and_full_strings():
    light = {"global_reconciled": True, "ledger": {"settled": 0},
             "candidate_lifecycle": {"terminals": {"accepted": 0}}}
    assert "global_reconciled=True" in build_validation_light(light)
    assert "stop_conditions=" in build_validation_full(light, status={})