"""Pytest bootstrap for the Hermes Trading Engine plugin tests.

Puts the plugin root (the directory that contains the ``engine`` package) on
``sys.path`` so ``import engine...`` works when these tests are run directly
(``pytest plugins/hermes-trading-engine/tests``), and pins a writable temp
data dir + clears Grok credentials before the ``engine`` package is imported.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

# Engine config reads these at import time — set safe values up front.
os.environ.setdefault("HTE_DATA_DIR", tempfile.mkdtemp(prefix="hte-test-data-"))
os.environ["HTE_AUTOTRADE"] = "0"
os.environ["HTE_MODE"] = "paper"
for _k in ("GROK_API_KEY", "XAI_API_KEY"):
    os.environ.pop(_k, None)

# Tier-1 baseline_cohort_gate defaults ON in production; legacy integration tests tick early
# (TTC >>240s) and lack edge/CEX tags — disable unless a test passes baseline_cohort_gate_enabled=True.
import dataclasses

import engine.pulse.engine as _engine_mod

_OriginalPulseConfig = _engine_mod.PulseConfig


@dataclasses.dataclass
class _PulseConfigTestDefault(_OriginalPulseConfig):
    """Relaxed defaults for legacy integration tests (not testing production gates)."""
    baseline_cohort_gate_enabled: bool = False
    baseline_up_tv_gate_enabled: bool = False
    selectivity_min_samples: int = 30
    # WS2 UP hard-block breaks synthetic rising-price integration paths; gate tests opt in.
    directional_block_up_until_promoted: bool = False
    directional_require_winning_bucket: bool = False
    # No MTF stub in most integration harnesses — conflict gate would veto every candidate.
    tv_mtf_conflict_gate_enabled: bool = False


_engine_mod.PulseConfig = _PulseConfigTestDefault
