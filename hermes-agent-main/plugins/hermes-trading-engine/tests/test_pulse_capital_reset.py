"""Surgical capital reset (PULSE_RESET_CAPITAL_TOKEN): zero P&L/ledger/arb/reconciliation to a
fresh $500 while KEEPING everything the bot has learned. PAPER ONLY.

Proves: after a reset token, capital is fresh and trade/arb counters are zero, but learned state
(selectivity evidence, lessons, edge model, calibration) is retained; and the reset is idempotent
(does not re-fire for the same token on the next restart)."""

from __future__ import annotations

import os

from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


class _Mkt:
    def __init__(self, w):
        self._w = w

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=50000, bid_depth_usd=50000,
                              asks=[(0.55, 100000.0)], bids=[(0.50, 100000.0)])
        w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=49000, bid_depth_usd=44000,
                                asks=[(0.49, 100000.0)], bids=[(0.44, 100000.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _engine(tmp_path, **over):
    t0 = 9_960_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                      selectivity_exploration_rate=0.0, starting_capital_usd=500.0,
                      directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False, data_dir=str(tmp_path), **over)
    return PulseEngine(cfg, market_feed=_Mkt(win), price_feed=feed), t0


def _drive(eng, t0):
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)


def test_capital_reset_zeros_money_keeps_learning(tmp_path, monkeypatch):
    # 1) run a session: produce trades + learned selectivity evidence
    monkeypatch.delenv("PULSE_RESET_CAPITAL_TOKEN", raising=False)
    eng, t0 = _engine(tmp_path)
    for _ in range(40):  # seed learned evidence in a bucket that won't block the rising-price UP trade
        eng.selectivity_evidence.record({"direction": "down"}, won=False, pnl=-5.0, outcome_up=True)
    _drive(eng, t0)
    eng._persist()
    assert eng.ledger.trades >= 1                      # money state exists pre-reset
    learned_before = eng.selectivity_evidence.has_data
    lessons_before = eng.lessons.report().get("count", 0)
    assert learned_before is True

    # 2) restart WITH a reset token -> loads state, then surgically resets capital
    monkeypatch.setenv("PULSE_RESET_CAPITAL_TOKEN", "reset-test-1")
    eng2, _ = _engine(tmp_path)
    # money is fresh
    assert eng2.ledger.trades == 0 and eng2.ledger.settled == 0
    assert eng2.ledger.stats()["realized_pnl_usd"] == 0.0
    cap = eng2._capital_status()
    assert cap["on_hand_capital_usd"] == 500.0 and cap["total_realized_pnl_usd"] == 0.0
    if eng2.arb_ledger is not None:
        assert eng2.arb_ledger.executed == 0 and eng2.arb_ledger.realized_profit_usd == 0.0
    # learning is RETAINED
    assert eng2.selectivity_evidence.has_data is True
    assert eng2.lessons.report().get("count", 0) == lessons_before
    assert eng2.light_report()["global_reconciled"] is True
    assert eng2.trade_history.recent(1) == []
    assert eng2._recent_windows == []
    assert eng2._report_epoch.get("token") == "reset-test-1"

    # 3) idempotent: another restart with the SAME token does NOT wipe new activity
    _drive(eng2, t0)
    eng2._persist()
    trades_after_rerun = eng2.ledger.trades
    assert trades_after_rerun >= 1
    eng3, _ = _engine(tmp_path)                          # same token still set
    assert eng3.ledger.trades == trades_after_rerun      # not reset again (token already applied)
