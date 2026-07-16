"""Integration tests — autonomy self-adjust loops fire correctly."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from autonomy.cbpf import ContinualBayesianFusion
from autonomy.freeze import FROZEN_KEYS, assert_mutable_only
from autonomy.mchb import MetaContextualBandit, build_context_from_meta
from autonomy.orchestrator import autonomy_tick, on_settlement
from autonomy.rasp import detect_regimes, get_rasp, synthetic_hard_examples
from autonomy.registry import ModelRegistry
from autonomy.rgmc import RiskGuardian
from autonomy.schemas import AutonomyState, SettlementReward


def test_freeze_blocks_min_edge():
    with pytest.raises(PermissionError):
        assert_mutable_only({"min_edge": 0.05, "swarm_weight": 0.7})
    ok = assert_mutable_only({"swarm_weight": 0.65, "size_multiplier": 0.9})
    assert "swarm_weight" in ok
    assert "min_edge" not in FROZEN_KEYS or "min_edge" in FROZEN_KEYS


def test_mchb_decide_and_update(tmp_path: Path):
    b = MetaContextualBandit(path=tmp_path / "mchb.json")
    ctx = build_context_from_meta(timeframe="5m", momentum=0.5, dislocation=0.08)
    d1 = b.decide(ctx)
    assert d1.arm in ("exploit", "explore", "skip")
    b.update(ctx, d1.arm, 1.0)
    assert b.path.is_file()
    d2 = b.decide(ctx)
    assert d2.family.startswith("fam:")


def test_cbpf_refit_improves_weights(tmp_path: Path):
    f = ContinualBayesianFusion(path=tmp_path / "cbpf.json")
    rng = np.random.default_rng(0)
    for i in range(30):
        y = i % 3 != 0
        comps = {
            "good": 0.8 if y else 0.2,
            "noise": float(rng.uniform(0.3, 0.7)),
        }
        f.update(comps, resolved_yes=y, p_market=0.5)
    assert f.n_updates == 30
    assert f.last_metrics.get("brier", 1) < 0.25
    fused = f.fuse({"good": 0.75, "noise": 0.5}, p_market=0.5)
    assert 0.05 <= fused <= 0.95


def test_rgmc_tighten_only():
    rg = RiskGuardian(AutonomyState(equity=2000, peak_equity=2000))
    for _ in range(20):
        rg.observe_settlement(won=False, pnl_usd=-20, equity=rg.state.equity - 20)
    assert rg.state.soft_kappa_scale < 1.0
    assert rg.state.size_multiplier < 1.0
    # Never above 1
    assert rg.state.soft_kappa_scale <= 1.0


def test_rasp_regime_and_hard_examples():
    rng = np.random.default_rng(1)
    rets = rng.normal(0, 0.001, size=80).tolist()
    det = detect_regimes(rets)
    assert det["active"] in ("low", "mid", "high")
    hard = synthetic_hard_examples([100.0 + i * 0.1 for i in range(50)], n=4)
    assert len(hard) == 4


def test_registry_shadow_promote_rollback(tmp_path: Path):
    reg = ModelRegistry(root=tmp_path / "models")
    card = reg.register("fusion", {"swarm_weight": 0.7, "market_blend": 0.3}, {"wr": 0.9})
    assert card.status.value == "shadow"
    for _ in range(100):
        st = reg.record_shadow_trade(won=True)
    assert st["ready"] is True
    promoted = reg.promote()
    assert promoted is not None
    assert promoted.status.value == "prod"
    # Second shadow → promote (retires first) → rollback restores first
    reg.register("fusion", {"swarm_weight": 0.6, "market_blend": 0.4}, {"wr": 0.9})
    for _ in range(100):
        reg.record_shadow_trade(won=True)
    second = reg.promote()
    assert second is not None
    restored = reg.rollback()
    assert restored is not None
    assert restored.status.value == "prod"


def test_settlement_reward_unit():
    r = SettlementReward(pnl_usd=50, size_usd=100, won=True, brier=0.05)
    assert 0.5 <= r.as_unit_reward() <= 1.0
    r2 = SettlementReward(pnl_usd=-80, size_usd=100, won=False)
    assert r2.as_unit_reward() < 0.5


def test_on_settlement_hook(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_PAPER_DIR", str(tmp_path / "paper"))
    monkeypatch.setenv("HERMES_PAPER_ONLY", "1")

    class STL:
        won = True
        pnl_usd = 25.0
        size_usd = 100.0
        notes = "bandit_arm=exploit model_q=0.72 pm_implied_up=0.40"
        timeframe = "5m"
        hourly_bucket = 14
        direction = "UP"

    out = on_settlement(STL())
    assert out.get("ok") is True
    assert out.get("reward", 0) > 0.5


def test_autonomy_tick_smoke(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_PAPER_DIR", str(tmp_path / "paper"))
    monkeypatch.setenv("HERMES_PAPER_ONLY", "1")
    monkeypatch.setenv("HERMES_EHO_FAST", "1")
    # Avoid heavy EHO in unit test — just ingest/rasp path
    from autonomy.orchestrator import load_autonomy_state, save_autonomy_state

    st = load_autonomy_state()
    st.last_eho_n = 10_000  # suppress EHO
    st.last_eho_at = "2099-01-01T00:00:00+00:00"
    save_autonomy_state(st)
    summary = autonomy_tick(force_nightly=False, force_eho=False)
    assert "state" in summary


def test_eho_never_proposes_frozen():
    from autonomy.eho import _decode, _encode

    z = _encode({"swarm_weight": 0.7, "soft_kappa_scale": 0.9, "size_multiplier": 0.8,
                 "max_conviction_boost": 0.05, "explore_rate": 0.1})
    params = assert_mutable_only(_decode(z))
    for k in ("min_edge", "min_conviction", "kappa_base", "mode"):
        assert k not in params
