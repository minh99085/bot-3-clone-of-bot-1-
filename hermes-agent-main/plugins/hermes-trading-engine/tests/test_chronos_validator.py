"""Tests for CHRONOS pre-decision validator."""

from __future__ import annotations

from engine.pulse.chronos_validator import (
    ChronosConfig,
    ChronosValidator,
    breakeven_wr_at_ask,
    chronos_verdict_score,
    context_key,
    kelly_binary_fraction,
    normalize_positions,
    wilson_lb,
)


def _pos(entry_ts, won, pnl, side="up", ask=0.50, slug="btc-up-or-down-15m", ws=900):
    return {
        "status": "settled",
        "entry_ts": entry_ts,
        "opened_at": entry_ts,
        "won": won,
        "pnl_usd": pnl,
        "side": side,
        "entry_price": ask,
        "research": {"series_slug": slug, "window_seconds": ws, "entry_ttc_s": ws * 0.5},
    }


def test_breakeven_and_kelly():
    assert breakeven_wr_at_ask(0.55) == 0.55
    assert kelly_binary_fraction(0.60, 0.50) > 0


def test_cvs_penalizes_below_breakeven():
    low = chronos_verdict_score(wilson_lb_wr=0.40, ask=0.55, cohort_n=10)
    high = chronos_verdict_score(wilson_lb_wr=0.62, ask=0.50, cohort_n=10)
    assert high > low


def test_validate_trade_blocks_losing_cohort():
    v = ChronosValidator(ChronosConfig(
        enabled=True, min_cohort_n=4, exploration_rate=0.0, block_margin=0.02))
    ctx = context_key(asset="btc", lane="15m", side="up", ask=0.55, ttc_s=450, window_seconds=900)
    positions = []
    for i in range(8):
        positions.append(_pos(1000 + i, False, -5.0, ask=0.55))
    cert = v.validate_trade(
        positions=positions, asset="btc", lane="15m", side="up", ask=0.55,
        now=2000, ttc_s=450, window_seconds=900)
    assert cert.cohort_n == 8
    assert cert.verdict == "block"
    assert cert.wilson_lb < 0.55


def test_validate_trade_proceeds_winning_cohort():
    v = ChronosValidator(ChronosConfig(enabled=True, min_cohort_n=4, exploration_rate=0.0))
    positions = [_pos(1000 + i, True, 4.0, ask=0.50) for i in range(10)]
    cert = v.validate_trade(
        positions=positions, asset="btc", lane="15m", side="up", ask=0.50,
        now=2000, ttc_s=450, window_seconds=900)
    assert cert.verdict == "proceed"
    assert cert.cvs > 0


def test_walk_forward_block_replay_chronological():
    v = ChronosValidator()
    rows = normalize_positions([
        _pos(1, False, -5, ask=0.55),
        _pos(2, True, 4, ask=0.50),
        _pos(3, False, -5, ask=0.55),
        _pos(4, False, -5, ask=0.55),
        _pos(5, True, 4, ask=0.50),
    ])
    ctx = context_key(asset="btc", lane="15m", side="up", ask=0.55, ttc_s=450, window_seconds=900)

    def block(row, _hist):
        return row.get("context") == ctx

    rep = v.walk_forward_block_replay(rows, should_block=block)
    assert rep["replayed"] == 5
    assert rep["trades_rejected"] >= 2
    assert rep["losses_avoided"] >= 1


def test_policy_veto_loosen_on_bad_holdout():
    v = ChronosValidator(ChronosConfig(enabled=True, kill_wr=0.55, holdout_fraction=0.4))
    positions = []
    # mostly losses in holdout
    for i in range(10):
        positions.append(_pos(100 + i, i < 2, -5.0 if i >= 2 else 4.0))
    out = v.validate_policy_action(positions=positions, action="loosen")
    assert out["approved"] is False
    assert "kill" in out["reason"]


def test_cold_probe_when_no_history():
    v = ChronosValidator(ChronosConfig(enabled=True, min_cohort_n=4, exploration_rate=0.0))
    cert = v.validate_trade(
        positions=[], asset="btc", lane="15m", side="up", ask=0.50, now=1000)
    assert cert.verdict == "probe"
    assert cert.cohort_n == 0
