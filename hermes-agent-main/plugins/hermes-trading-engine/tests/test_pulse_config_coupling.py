"""Config coupling: TV context max TTC vs baseline cohort."""

from engine.pulse.config_coupling import (
    apply_context_cohort_coupling,
    evaluate_context_cohort_coupling,
    required_tv_context_max_ttc_s,
    window_seconds_for_slugs,
)


def test_required_min_for_dual_market():
    req = required_tv_context_max_ttc_s(
        cohort_ttc_min_s=180.0,
        cohort_ttc_max_s=240.0,
        window_seconds_list=[300, 900],
    )
    assert req > 720.0
    assert req < 725.0


def test_deadlock_at_180_dual_market():
    rep = evaluate_context_cohort_coupling(
        baseline_cohort_enabled=True,
        tv_context_enabled=True,
        configured_context_max_ttc_s=180.0,
        cohort_ttc_min_s=180.0,
        cohort_ttc_max_s=240.0,
        window_seconds_list=[300, 900],
        auto_clamp=False,
    )
    assert rep["active"] is True
    assert rep["configured_ok"] is False
    assert rep["per_window"][1]["deadlocked"] is True


def test_900_configured_ok():
    rep = evaluate_context_cohort_coupling(
        baseline_cohort_enabled=True,
        tv_context_enabled=True,
        configured_context_max_ttc_s=900.0,
        cohort_ttc_min_s=180.0,
        cohort_ttc_max_s=240.0,
        window_seconds_list=[300, 900],
    )
    assert rep["ok"] is True
    assert rep["configured_ok"] is True
    assert rep["auto_clamped"] is False


def test_auto_clamp_raises_effective():
    effective, rep = apply_context_cohort_coupling(
        baseline_cohort_enabled=True,
        tv_context_enabled=True,
        configured_context_max_ttc_s=180.0,
        cohort_ttc_min_s=180.0,
        cohort_ttc_max_s=240.0,
        window_seconds_list=[300, 900],
    )
    assert effective > 720.0
    assert rep["auto_clamped"] is True
    assert rep["ok"] is True


def test_inactive_when_gate_off():
    rep = evaluate_context_cohort_coupling(
        baseline_cohort_enabled=False,
        tv_context_enabled=True,
        configured_context_max_ttc_s=120.0,
        cohort_ttc_min_s=180.0,
        cohort_ttc_max_s=240.0,
        window_seconds_list=[300],
    )
    assert rep["active"] is False
    assert rep["ok"] is True


def test_window_seconds_from_slugs():
    ws = window_seconds_for_slugs(["btc-up-or-down-5m", "btc-up-or-down-15m"])
    assert ws == [300, 900]