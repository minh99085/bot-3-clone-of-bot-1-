"""Env coupling rules — keep TV context max-TTC compatible with baseline cohort bands.

The context gate blocks when ``ttc_s >= max_ttc_s``. The baseline cohort gate only allows
``cohort_min <= ttc <= cohort_max`` scaled by ``window_seconds / 300``. If
``max_ttc_s <= cohort_min * scale`` (or is below the scaled cohort max), the two gates
deadlock and the quant path never trades.
"""

from __future__ import annotations

from typing import Optional, Sequence

from engine.pulse.markets import SERIES_DEFAULTS, WINDOW_SECONDS

# Context gate uses ``ttc >= max``; cohort allows ``ttc <= cohort_max``. Need strict headroom.
_TTC_EPS_S = 1.0


def window_seconds_for_slugs(slugs: Sequence[str]) -> list[int]:
    out: list[int] = []
    for slug in slugs:
        key = str(slug or "").strip()
        defaults = SERIES_DEFAULTS.get(key)
        out.append(int(defaults["window_seconds"]) if defaults else WINDOW_SECONDS)
    return out or [WINDOW_SECONDS]


def required_tv_context_max_ttc_s(
    *,
    cohort_ttc_min_s: float,
    cohort_ttc_max_s: float,
    window_seconds_list: Sequence[int],
) -> float:
    """Minimum context max TTC so every active window keeps cohort-band overlap."""
    scales = [float(ws) / 300.0 for ws in window_seconds_list] or [1.0]
    # Overlap with full cohort band on each window: block at >= max, so max > cohort_max * scale.
    per_window_max = [float(cohort_ttc_max_s) * s + _TTC_EPS_S for s in scales]
    # Any trade at cohort min also needs max > cohort_min * scale (>= blocks the minimum).
    per_window_min = [float(cohort_ttc_min_s) * s + _TTC_EPS_S for s in scales]
    return max(max(per_window_max), max(per_window_min))


def evaluate_context_cohort_coupling(
    *,
    baseline_cohort_enabled: bool,
    tv_context_enabled: bool,
    configured_context_max_ttc_s: Optional[float],
    cohort_ttc_min_s: float,
    cohort_ttc_max_s: float,
    window_seconds_list: Sequence[int],
    auto_clamp: bool = True,
) -> dict:
    """Return coupling diagnostics for status API / health scans."""
    windows = [int(w) for w in window_seconds_list] or [WINDOW_SECONDS]
    active = bool(baseline_cohort_enabled and tv_context_enabled
                  and configured_context_max_ttc_s is not None)
    required = required_tv_context_max_ttc_s(
        cohort_ttc_min_s=cohort_ttc_min_s,
        cohort_ttc_max_s=cohort_ttc_max_s,
        window_seconds_list=windows,
    )
    configured = (float(configured_context_max_ttc_s)
                  if configured_context_max_ttc_s is not None else None)
    per_window = []
    for ws in windows:
        scale = float(ws) / 300.0
        band_min = float(cohort_ttc_min_s) * scale
        band_max = float(cohort_ttc_max_s) * scale
        deadlocked = (
            active and configured is not None
            and configured <= band_min
        )
        per_window.append({
            "window_seconds": ws,
            "cohort_ttc_band_s": [round(band_min, 1), round(band_max, 1)],
            "deadlocked": deadlocked,
        })
    configured_ok = (not active) or (configured is not None and configured >= required)
    effective = configured
    auto_clamped = False
    if active and configured is not None and configured < required:
        if auto_clamp:
            effective = required
            auto_clamped = True
    effective_ok = (not active) or (effective is not None and effective >= required)
    return {
        "rule": "tv_context_max_ttc_s_must_exceed_scaled_baseline_cohort_band",
        "active": active,
        "ok": effective_ok,
        "configured_ok": configured_ok,
        "auto_clamped": auto_clamped,
        "configured_s": configured,
        "required_min_s": round(required, 1) if active else None,
        "effective_s": effective,
        "cohort_ttc_base_s": [float(cohort_ttc_min_s), float(cohort_ttc_max_s)],
        "window_seconds": windows,
        "per_window": per_window,
        "fix_hint": (
            f"Set PULSE_TV_CONTEXT_MAX_TTC_S >= {int(required)} "
            f"(or disable PULSE_BASELINE_COHORT_GATE / PULSE_TV_CONTEXT_GATE)."
            if active and not configured_ok else None
        ),
    }


def apply_context_cohort_coupling(
    *,
    baseline_cohort_enabled: bool,
    tv_context_enabled: bool,
    configured_context_max_ttc_s: Optional[float],
    cohort_ttc_min_s: float,
    cohort_ttc_max_s: float,
    window_seconds_list: Sequence[int],
) -> tuple[Optional[float], dict]:
    """Return (effective_max_ttc_s, report) for TradingViewContextGate."""
    report = evaluate_context_cohort_coupling(
        baseline_cohort_enabled=baseline_cohort_enabled,
        tv_context_enabled=tv_context_enabled,
        configured_context_max_ttc_s=configured_context_max_ttc_s,
        cohort_ttc_min_s=cohort_ttc_min_s,
        cohort_ttc_max_s=cohort_ttc_max_s,
        window_seconds_list=window_seconds_list,
        auto_clamp=True,
    )
    if not report["active"]:
        return configured_context_max_ttc_s, report
    return report["effective_s"], report