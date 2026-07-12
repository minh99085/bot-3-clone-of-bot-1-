"""Strict execution-quality gate for the BTC pulse (orderbook reality, not midpoint).

Proves: EV is computed from the VWAP fill over real ask depth; midpoint/top-of-book
profitable trades are REJECTED when VWAP/slippage makes EV negative; every rejection reason
fires; the paper ledger reconciles candidates = accepted + rejected (fills = accepted)."""

from __future__ import annotations

from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.execution_gate import (evaluate_execution, vwap_fill, ExecResult,
                                          WIDE_SPREAD, INSUFFICIENT_DEPTH, NEGATIVE_EV,
                                          TOO_CLOSE, MIN_SIZE_OR_TICK, PARTIAL_FILL_RISK,
                                          UNDERDOG_PRICE)
from engine.pulse.executor import PulseLedger
from engine.pulse.engine import PulseEngine, PulseConfig
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol


def _book(best_bid, best_ask, asks, bids=None):
    bids = bids or [(best_bid, 1000.0)]
    return OrderBook(best_bid=best_bid, best_ask=best_ask,
                     ask_depth_usd=round(sum(p * s for p, s in asks), 2),
                     bid_depth_usd=round(sum(p * s for p, s in bids), 2),
                     asks=asks, bids=bids)


def test_vwap_fill_walks_the_ladder():
    vwap, spent, shares, fully = vwap_fill([(0.50, 2.0), (0.95, 1000.0)], 10.0)
    assert fully and abs(spent - 10.0) < 1e-9
    # $1 at 0.50 (2 sh) + $9 at 0.95 (9.473 sh) -> vwap ~0.872
    assert 0.86 < vwap < 0.88
    vwap2, spent2, _, fully2 = vwap_fill([(0.50, 10.0)], 10.0)   # only $5 of depth
    assert not fully2 and abs(spent2 - 5.0) < 1e-9


# --- ACCEPTANCE #5: midpoint-profitable but VWAP makes EV negative -> rejected ----------- #
def test_midpoint_profitable_but_vwap_negative_is_rejected():
    # mid 0.495 and top-of-book 0.50 both look profitable vs fair 0.55, but depth is thin at
    # 0.50 then jumps to 0.95, so the VWAP fill for $10 is ~0.87 -> EV after slippage negative.
    book = _book(0.49, 0.50, asks=[(0.50, 2.0), (0.95, 1000.0)])
    r = evaluate_execution(side="up", book=book, outcome_prob=0.55, size_usd=10.0,
                           tick_size=0.01, ttc_s=120.0, max_spread=0.06, min_depth_usd=1.0,
                           min_order_usd=1.0, max_depth_consume_frac=0.9)
    assert r.accepted is False and r.reason == NEGATIVE_EV
    assert r.ev_at_mid is not None and r.ev_at_mid > 0          # midpoint said "profitable"
    assert r.ev_after_slippage is not None and r.ev_after_slippage < 0   # reality: negative
    assert r.slippage > 0


def test_accept_when_depth_deep_and_ev_survives():
    book = _book(0.49, 0.50, asks=[(0.50, 1000.0)])            # deep at 0.50
    r = evaluate_execution(side="up", book=book, outcome_prob=0.62, size_usd=10.0,
                           tick_size=0.01, ttc_s=120.0)
    assert r.accepted is True and r.reason == "accepted"
    assert abs(r.fill_price - 0.50) < 1e-6 and r.ev_after_slippage > 0


def test_underdog_price_floor_rejects_cheap_side():
    # buying a side whose VWAP fill is below the floor (the underdog) is rejected even if EV looks
    # positive on the bot's (overconfident) probability — the price is the better estimate.
    book = _book(0.29, 0.30, asks=[(0.30, 1000.0)])
    r = evaluate_execution(side="up", book=book, outcome_prob=0.45, size_usd=10.0, tick_size=0.01,
                           ttc_s=120.0, min_fill_price=0.50)
    assert r.accepted is False and r.reason == UNDERDOG_PRICE and r.vwap is not None and r.vwap < 0.5
    # the SAME cheap side passes when the floor is disabled (proven-edge path uses min_fill_price=0)
    r2 = evaluate_execution(side="up", book=book, outcome_prob=0.45, size_usd=10.0, tick_size=0.01,
                            ttc_s=120.0, min_fill_price=0.0)
    assert r2.accepted is True and r2.reason == "accepted"
    # a favourite (fill >= floor) is unaffected by the floor
    fav = _book(0.59, 0.60, asks=[(0.60, 1000.0)])
    r3 = evaluate_execution(side="up", book=fav, outcome_prob=0.70, size_usd=10.0, tick_size=0.01,
                            ttc_s=120.0, min_fill_price=0.50)
    assert r3.accepted is True and r3.reason == "accepted"


# --- every explicit rejection reason fires ---------------------------------------------- #
def test_reason_too_close_to_resolution():
    book = _book(0.49, 0.50, asks=[(0.50, 1000.0)])
    assert evaluate_execution(side="up", book=book, outcome_prob=0.9, size_usd=10.0,
                              tick_size=0.01, ttc_s=2.0, min_seconds_to_close=4.0).reason == TOO_CLOSE


def test_reason_min_size_or_tick():
    book = _book(0.49, 0.50, asks=[(0.50, 1000.0)])
    assert evaluate_execution(side="up", book=book, outcome_prob=0.9, size_usd=0.5,
                              tick_size=0.01, ttc_s=120.0, min_order_usd=1.0).reason == MIN_SIZE_OR_TICK
    off_tick = _book(0.49, 0.505, asks=[(0.505, 1000.0)])      # 0.505 not on 0.01 grid
    assert evaluate_execution(side="up", book=off_tick, outcome_prob=0.9, size_usd=10.0,
                              tick_size=0.01, ttc_s=120.0).reason == MIN_SIZE_OR_TICK


def test_reason_wide_spread():
    book = _book(0.30, 0.50, asks=[(0.50, 1000.0)])            # spread 0.20
    assert evaluate_execution(side="up", book=book, outcome_prob=0.9, size_usd=10.0,
                              tick_size=0.01, ttc_s=120.0, max_spread=0.06).reason == WIDE_SPREAD


def test_reason_insufficient_depth():
    book = _book(0.49, 0.50, asks=[(0.50, 1.0)])               # $0.50 total depth
    assert evaluate_execution(side="up", book=book, outcome_prob=0.9, size_usd=10.0,
                              tick_size=0.01, ttc_s=120.0, min_depth_usd=1.0).reason == INSUFFICIENT_DEPTH


def test_reason_stale_orderbook():
    from engine.pulse.execution_gate import STALE_ORDERBOOK
    book = _book(0.49, 0.50, asks=[(0.50, 1000.0)])
    book.ts = 1000.0
    # book 60s old vs 30s max -> stale
    r = evaluate_execution(side="up", book=book, outcome_prob=0.62, size_usd=10.0,
                           tick_size=0.01, ttc_s=120.0, now=1060.0, max_book_age_s=30.0)
    assert r.reason == STALE_ORDERBOOK
    # fresh book (1s old) is not stale
    book.ts = 1059.0
    r2 = evaluate_execution(side="up", book=book, outcome_prob=0.62, size_usd=10.0,
                            tick_size=0.01, ttc_s=120.0, now=1060.0, max_book_age_s=30.0)
    assert r2.reason != STALE_ORDERBOOK and r2.accepted is True


def test_reason_partial_fill_risk():
    # depth above the floor but cannot fully fill the $10 order -> partial fill risk
    book = _book(0.49, 0.50, asks=[(0.50, 12.0)])              # $6 total depth (>min, <size)
    assert evaluate_execution(side="up", book=book, outcome_prob=0.9, size_usd=10.0,
                              tick_size=0.01, ttc_s=120.0, min_depth_usd=1.0,
                              max_depth_consume_frac=0.9).reason == PARTIAL_FILL_RISK


def test_fee_can_turn_apparent_edge_negative():
    book = _book(0.49, 0.50, asks=[(0.50, 1000.0)])
    gross = evaluate_execution(side="up", book=book, outcome_prob=0.515, size_usd=10.0,
                               tick_size=0.01, ttc_s=120.0, taker_fee_rate=0.0)
    net = evaluate_execution(side="up", book=book, outcome_prob=0.515, size_usd=10.0,
                             tick_size=0.01, ttc_s=120.0, taker_fee_rate=0.07)
    assert gross.accepted is True
    assert net.accepted is False and net.reason == NEGATIVE_EV
    assert abs(net.fee_per_share - 0.0175) < 1e-9
    assert abs(net.fee_usd - 0.35) < 1e-9


# --- ledger reconciliation -------------------------------------------------------------- #
def test_ledger_exec_reconciliation_balances():
    led = PulseLedger()
    led.record_exec(True, "accepted")
    led.record_exec(False, NEGATIVE_EV)
    led.record_exec(False, WIDE_SPREAD)
    led.record_exec(False, NEGATIVE_EV)
    s = led.exec_gate_stats()
    assert s["candidates"] == 4 and s["accepted"] == 1 and s["rejected_total"] == 3
    assert s["rejected"][NEGATIVE_EV] == 2 and s["rejected"][WIDE_SPREAD] == 1
    assert s["fills"] == 1 and s["reconciled"] is True


class _ThinMarket:
    """A window whose book looks tradeable at top-of-book but cannot fully fill the order size
    (ladder depth < size), so the execution gate must reject on partial-fill risk."""
    def active_windows(self, now=None, **kw):
        return [self._w]
    def __init__(self, w):
        self._w = w
    def hydrate_books(self, w):
        w.up_book = _book(0.49, 0.50, asks=[(0.50, 6.0)])      # only $3 of depth vs $10 order
        w.down_book = _book(0.49, 0.50, asks=[(0.50, 6.0)])
        return w
    def fetch_resolution(self, market_id):
        return None


def test_engine_exec_gate_rejects_and_reconciles(tmp_path):
    t0 = 7_000_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    eng = PulseEngine(PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False,
                                  basis_buffer=0.0, min_seconds_since_open=0.0,
                                  sigma_trust_floor=0.0, min_vol_samples=2,
                                  exec_max_depth_consume_frac=0.9, data_dir=str(tmp_path)),
                      market_feed=_ThinMarket(win), price_feed=feed)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(8):
        eng.tick(now=t0 + 2 + k * 5)
    s = eng.ledger.exec_gate_stats()
    assert eng.ledger.trades == 0                       # thin ladder -> gate blocked the trade
    assert s["candidates"] >= 1 and s["accepted"] == 0
    assert s["rejected"][PARTIAL_FILL_RISK] >= 1 and s["reconciled"] is True
