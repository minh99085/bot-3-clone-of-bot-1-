"""Provenance artifacts for full-report generation (Prompt-2 R12).

Writes MANIFEST.txt, validation_full.txt, validation_light.txt with git/docker/env/runtime proof.
PAPER ONLY.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional


def _git_proof() -> dict:
    try:
        root = Path(__file__).resolve().parents[2]
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True,
        ).strip()
        dirty = subprocess.call(
            ["git", "diff", "--quiet", "HEAD"], cwd=root, stderr=subprocess.DEVNULL,
        ) != 0
        return {"commit": commit, "dirty": dirty}
    except Exception:  # noqa: BLE001
        return {"commit": None, "dirty": None}


def _docker_proof() -> dict:
    hostname = os.environ.get("HOSTNAME", "")
    return {
        "hostname": hostname or None,
        "image": os.environ.get("HTE_IMAGE_ID") or os.environ.get("IMAGE_ID"),
        "compose_project": os.environ.get("COMPOSE_PROJECT_NAME"),
    }


def _env_proof() -> dict:
    keys = (
        "PULSE_GROK_DECIDER_MODE", "PULSE_VERIFIER_ENABLED", "PULSE_DIRECTIONAL_ENABLED",
        "PULSE_DIRECTIONAL_REQUIRE_WINNING", "PULSE_LEARNING_ENABLED",
        "GROK_OVERLAY_ENABLED", "PULSE_TV_CONTEXT_GATE",
    )
    return {k: os.environ.get(k) for k in keys}


def build_manifest(
    *,
    light_report: dict,
    status: Optional[dict] = None,
    ledger: Optional[dict] = None,
) -> str:
    status = status or {}
    ledger = ledger or {}
    lines = [
        "Hermes BTC 5-min Pulse — PROVENANCE MANIFEST",
        "paper_only=true live_trading_enabled=false",
        "generated_utc=%s" % time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "",
        "[git]",
        "commit=%s" % (_git_proof().get("commit") or "unknown"),
        "dirty=%s" % _git_proof().get("dirty"),
        "",
        "[docker]",
    ]
    for k, v in _docker_proof().items():
        lines.append("%s=%s" % (k, v))
    lines.extend(["", "[env_flags]"])
    for k, v in _env_proof().items():
        lines.append("%s=%s" % (k, v))
    lines.extend([
        "",
        "[runtime]",
        "ticks=%s" % status.get("ticks"),
        "global_reconciled=%s" % (light_report or {}).get("global_reconciled"),
        "settled=%s" % ((ledger.get("stats") or ledger).get("settled")
                        if isinstance(ledger, dict) else None),
        "",
        "[artifacts]",
        "btc_pulse_status.json",
        "btc_pulse_ledger.json",
        "btc_pulse_light_report.json",
        "btc_pulse_meta_bundle.json",
        "report.md",
        "report.docx",
        "btc_pulse_score_history.json",
        "LESSONS.md",
        "STATE.md",
        "MANIFEST.txt",
        "validation_full.txt",
        "validation_light.txt",
    ])
    return "\n".join(lines) + "\n"


def build_validation_light(light_report: dict) -> str:
    lr = light_report or {}
    rec = lr.get("reconciliation") or {}
    lines = [
        "validation_light — integrity smoke checks",
        "global_reconciled=%s" % lr.get("global_reconciled"),
        "failed_checks=%s" % (rec.get("failed_checks") or []),
        "paper_only=%s" % (not lr.get("live_trading_enabled", True)),
        "settled=%s" % (lr.get("ledger") or {}).get("settled"),
        "accepted=%s" % (lr.get("candidate_lifecycle") or {}).get("terminals", {}).get("accepted"),
    ]
    guards = ((lr.get("execution_realistic_edge") or {}).get("payoff_guards") or {})
    if guards:
        lines.append("payoff_guards=%s" % json.dumps(guards))
    return "\n".join(lines) + "\n"


def build_validation_full(light_report: dict, status: Optional[dict] = None) -> str:
    lr = light_report or {}
    status = status or {}
    lines = [
        "validation_full — extended integrity + stop conditions",
        build_validation_light(lr).strip(),
        "",
        "stop_conditions=%s" % json.dumps(status.get("stop_conditions") or lr.get("stop_conditions")),
        "directional_allowlist=%s" % json.dumps(lr.get("directional_allowlist")),
        "learning=%s" % json.dumps({k: (lr.get("learning") or {}).get(k) for k in
                                    ("active", "weight", "reason", "market_benchmark")}),
        "kl_observe_only=%s" % json.dumps((lr.get("execution_realistic_edge") or {}).get("kl_model_vs_market")),
    ]
    return "\n".join(lines) + "\n"


def write_provenance_artifacts(
    data_dir: Path,
    *,
    light_report: dict,
    status: Optional[dict] = None,
    ledger: Optional[dict] = None,
) -> list[str]:
    """Write all provenance files; return paths written."""
    written = []
    for name, content in (
        ("MANIFEST.txt", build_manifest(light_report=light_report, status=status, ledger=ledger)),
        ("validation_light.txt", build_validation_light(light_report)),
        ("validation_full.txt", build_validation_full(light_report, status=status)),
    ):
        path = data_dir / name
        path.write_text(content, encoding="utf-8")
        written.append(name)
    return written