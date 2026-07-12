"""Tier-2 selectivity: PF floor + Benjamini-Hochberg."""

from engine.pulse.selectivity import (
    SelectivityEvidence, LearnedSelectivityGate, benjamini_hochberg, profit_factor_from_stat,
)


def test_profit_factor_blocks_without_pf_floor():
    ev = SelectivityEvidence()
    for i in range(50):
        won = i < 20
        ev.record({"hurst_regime": "noise"}, won=won, pnl=(3.0 if won else -5.0), outcome_up=False)
    gate = LearnedSelectivityGate(min_samples=50, min_profit_factor=0.85, exploration_rate=0.0)
    st = ev.stat("hurst_regime", "noise")
    assert profit_factor_from_stat(st) is not None
    assert profit_factor_from_stat(st) < 0.85
    res = gate.evaluate({"hurst_regime": "noise"}, ev)
    assert res["decision"] == "reject"


def test_benjamini_hochberg_controls_false_positives():
    pvals = [0.001, 0.04, 0.15, 0.5]
    flags = benjamini_hochberg(pvals, q=0.10)
    assert flags[0] is True
    assert sum(flags) <= 2


def test_live_block_audit_present():
    ev = SelectivityEvidence()
    for i in range(55):
        won = i < 18
        ev.record({"direction": "up"}, won=won, pnl=(2.0 if won else -5.0), outcome_up=won)
    gate = LearnedSelectivityGate(min_samples=50, min_profit_factor=0.85, exploration_rate=0.0)
    positions = [{"tags": {"direction": "up"}, "won": False, "pnl": -5.0} for _ in range(5)]
    rep = gate.report(evidence=ev, positions=positions)
    assert "live_block_audit" in rep
    assert "counterfactual" in rep