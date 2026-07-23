"""Registry v2 — drift math, slippage gate, verifier hard cap.

Motivated by the last-10h report: cheap-side fades 2/72 (fair ~20%, −4.3σ),
389bps average entry slippage, and one $200 ticket through a 2% cap.
"""

from __future__ import annotations

import pytest

from strategy.advanced_signals import (
    DRIFT_CLAMP_ANN,
    barrier_implied_up,
    barrier_implied_up_drift,
    drift_mu_ann,
)


# ── drift estimation ─────────────────────────────────────────────────────────

def test_drift_positive_on_rising_tape():
    times = [float(i) for i in range(200)]
    prices = [64000.0 * (1 + 0.00002 * i) for i in range(200)]
    mu = drift_mu_ann(prices, times)
    assert mu > 0


def test_drift_zero_on_flat_or_thin():
    times = [float(i) for i in range(200)]
    assert drift_mu_ann([64000.0] * 200, times) == 0.0
    assert drift_mu_ann([64000.0, 64001.0], [0.0, 1.0]) == 0.0
    assert drift_mu_ann([], []) == 0.0


def test_drift_clamped():
    times = [0.0, 60.0, 120.0, 180.0]
    prices = [64000.0, 66000.0, 68000.0, 70000.0]  # absurd 3-min pump
    assert drift_mu_ann(prices, times) == pytest.approx(DRIFT_CLAMP_ANN)
    down = [70000.0, 68000.0, 66000.0, 64000.0]
    assert drift_mu_ann(down, times) == pytest.approx(-DRIFT_CLAMP_ANN)


# ── drift barrier ────────────────────────────────────────────────────────────

def test_drift_barrier_reduces_to_plain_at_zero_mu():
    q0 = barrier_implied_up(64100.0, 64000.0, 0.8, 400.0)
    qd = barrier_implied_up_drift(64100.0, 64000.0, 0.8, 400.0, 0.0)
    assert qd == pytest.approx(q0, abs=1e-12)


def test_positive_drift_raises_q_negative_lowers():
    q0 = barrier_implied_up_drift(64000.0, 64000.0, 0.8, 400.0, 0.0)
    q_up = barrier_implied_up_drift(64000.0, 64000.0, 0.8, 400.0, 3.0)
    q_dn = barrier_implied_up_drift(64000.0, 64000.0, 0.8, 400.0, -3.0)
    assert q_up > q0 > q_dn


def test_drift_barrier_anti_fade_property():
    """The exact failure case: price collapsed below strike late in the window.

    Driftless barrier says the cheap UP side is 'not that dead' (fade-buyable);
    with the observed negative drift, q drops — the model stops calling the
    collapsed side cheap. This is the 2.8%-WR fix in one assertion. μ = −300
    is a falling tape (~17bps/3min, post-shrink) — a realistic collapse.
    """
    spot, strike = 63900.0, 64000.0   # −16bps with 5 min left
    q_plain = barrier_implied_up(spot, strike, 0.8, 300.0)
    q_drift = barrier_implied_up_drift(spot, strike, 0.8, 300.0, -300.0)
    assert q_plain > 0.10             # fade-buyable per the driftless model
    assert q_drift < q_plain - 0.05   # drift kills the fade materially


# ── slippage gate (pretrade) ─────────────────────────────────────────────────

def _sig():
    from hermes.models import (
        AllocationProposal,
        ConfidenceTier,
        Direction,
        EntryMode,
        Regime,
        Signal,
    )
    from hermes.substrategy import annotate_signal

    sig = annotate_signal(Signal(
        market_id="mkt_btc", slug="btc-updown-15m-1784601000",
        question="Bitcoin Up or Down", direction=Direction.UP,
        entry_mode=EntryMode.MISPRICING, confidence_tier=ConfidenceTier.A,
        conviction=0.8, fair_value=0.75, market_price=0.72, expected_edge=0.05,
        live_ev=0.04, regime=Regime.MEAN_REVERT, hourly_bucket=14,
        size_usd_suggested=40.0, entry_vwap_target=0.72,
        pre_entry_stability_ok=True, timeframe="15m", oracle_alignment=0.8,
        meta={"paper": True, "asset": "BTC", "mispricing_active": True},
    ))
    proposal = AllocationProposal(
        capital_usd=2000, weights={sig.substrategy_id: 0.5},
        diversification_ratio=1.2, concentration_hhi=0.5,
    )
    return sig, proposal


@pytest.mark.parametrize("pure", ["1", "0"])
def test_slippage_gate_blocks_thin_books(monkeypatch, pure):
    import hermes.pretrade as pt

    monkeypatch.setenv("HERMES_PURE_MODE", pure)
    monkeypatch.setenv("HERMES_SCOPE_BTC_UPDOWN_ONLY", "1")
    monkeypatch.setattr(pt, "_recalc_live_ev", lambda s: (0.05, 400.0, "slip=400bps"))
    sig, proposal = _sig()
    analysis = pt.analyze_signal(sig, proposal, bankroll=2000.0, lessons="", paper=True)
    assert analysis.skip
    assert any("slippage_gate" in r for r in analysis.reasons)


def test_slippage_gate_passes_tight_books(monkeypatch):
    import hermes.pretrade as pt

    monkeypatch.setenv("HERMES_PURE_MODE", "1")
    monkeypatch.setenv("HERMES_SCOPE_BTC_UPDOWN_ONLY", "1")
    monkeypatch.setattr(pt, "_recalc_live_ev", lambda s: (0.05, 80.0, "slip=80bps"))
    sig, proposal = _sig()
    analysis = pt.analyze_signal(sig, proposal, bankroll=2000.0, lessons="", paper=True)
    assert not analysis.skip
    assert analysis.recommended_size_usd == pytest.approx(40.0)


# ── verifier hard cap (the $200-ticket hole) ────────────────────────────────

def test_verifier_sizing_clamped_to_hard_cap(monkeypatch):
    from hermes.verifier import _sizing_ok

    monkeypatch.setenv("HERMES_MAX_TRADE_PCT", "0.02")
    sig, _ = _sig()
    # kelly-path signal that skipped the pretrade clamp: $200 on a $2k book
    sig.allocation_usd = 200.0
    sig.pretrade_skip = False
    ok, sized, detail = _sizing_ok(sig, {"capital_usd": 2000})
    assert ok
    assert sized == pytest.approx(40.0)  # 2% hard cap, not 10% MAX_SINGLE_POSITION
