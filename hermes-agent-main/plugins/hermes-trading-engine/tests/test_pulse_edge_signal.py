"""BTC Pulse Edge Signal layer (OBSERVE-ONLY): CEX basket momentum, stale-price divergence,
time-to-resolution + orderbook pressure buckets, and a bounded pulse_edge_score.

Proves: missing CEX feeds handled safely; stale classification correct; pulse_edge_score is
deterministic + bounded; bucket stats reconcile with the ledger; the edge signal cannot bypass the
execution gate; and the report fields exist.
"""

from __future__ import annotations

from engine.pulse.edge_signal import (CexBasket, classify_stale_divergence, ttc_bucket_edge,
                                       compute_pulse_edge_score, orderbook_pressure,
                                       EdgeSignalEngine, EdgeSignalLearner, STALE_CLASSES)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


# ------------------------------- CEX basket (missing feeds safe) --------------------------- #
def test_basket_missing_feeds_safe():
    b = CexBasket(["binance_btcusdt", "coinbase_btcusd", "kraken_btcusd"])
    # no data at all -> momentum is all-None, coverage lists everything missing, never raises
    m = b.momentum(now=1000.0)
    assert m["returns"]["r30s"] is None and m["velocity"] is None
    assert m["exchange_agreement"] is None and m["basket_direction"] is None
    cov = m["coverage"]
    assert cov["n_present"] == 0 and set(cov["missing"]) == set(b.members)


def test_basket_momentum_and_agreement():
    b = CexBasket(["binance_btcusdt", "coinbase_btcusd"], stale_s=300.0)
    t = 10_000.0
    # both exchanges rising in lockstep over 60s
    for dt in range(0, 65, 5):
        b.observe("binance_btcusdt", 64000.0 * (1 + 0.0001 * dt), t + dt)
        b.observe("coinbase_btcusd", 63990.0 * (1 + 0.0001 * dt), t + dt)
    m = b.momentum(now=t + 60)
    assert m["returns"]["r30s"] is not None and m["returns"]["r30s"] > 0   # rising
    assert m["basket_direction"] == "up"
    assert m["exchange_agreement"] == 1.0                                   # both agree up
    cov = m["coverage"]
    assert cov["n_present"] == 2 and not cov["missing"]


def test_basket_reports_missing_reason():
    b = CexBasket(["binance_btcusdt", "kraken_btcusd"])
    b.observe("binance_btcusdt", 64000.0, 1.0)
    b.observe("kraken_btcusd", None, 1.0, missing_reason="disabled_by_config")
    cov = b.coverage(now=2.0)
    assert "binance_btcusdt" in cov["present"]
    assert cov["missing"]["kraken_btcusd"] == "disabled_by_config"


# ------------------------------- stale-price divergence classification --------------------- #
def test_stale_divergence_classification():
    # CEX pressure UP but Polymarket YES still ~0.5 -> stale_polymarket_up
    assert classify_stale_divergence(cex_return=0.002, poly_yes=0.50) == "stale_polymarket_up"
    # CEX pressure DOWN but Polymarket YES still ~0.5 -> stale_polymarket_down
    assert classify_stale_divergence(cex_return=-0.002, poly_yes=0.50) == "stale_polymarket_down"
    # CEX up AND Polymarket already priced up -> already_priced
    assert classify_stale_divergence(cex_return=0.002, poly_yes=0.70) == "already_priced"
    assert classify_stale_divergence(cex_return=-0.002, poly_yes=0.30) == "already_priced"
    # tiny CEX move -> not_stale
    assert classify_stale_divergence(cex_return=0.00001, poly_yes=0.50) == "not_stale"
    # missing inputs -> insufficient_data
    assert classify_stale_divergence(cex_return=None, poly_yes=0.5) == "insufficient_data"
    assert classify_stale_divergence(cex_return=0.002, poly_yes=None) == "insufficient_data"


def test_ttc_buckets():
    assert ttc_bucket_edge(290) == "240_300s" and ttc_bucket_edge(200) == "180_240s"
    assert ttc_bucket_edge(120) == "90_180s" and ttc_bucket_edge(60) == "30_90s"
    assert ttc_bucket_edge(10) == "0_30s" and ttc_bucket_edge(None) == "na"


# ------------------------------- pulse_edge_score deterministic + bounded ------------------ #
def _score_inputs(**over):
    base = dict(tv_strength=0.9, cex_agreement=1.0, stale_class="stale_polymarket_up",
                ob_imbalance=0.3, basket_direction="up", hurst_regime="trending",
                spread=0.01, ask_depth_usd=5000.0, ttc_s=240.0, realized_vol=2e-5)
    base.update(over)
    return base


def test_pulse_edge_score_deterministic_and_bounded():
    a = compute_pulse_edge_score(**_score_inputs())
    b = compute_pulse_edge_score(**_score_inputs())
    assert a == b                                   # deterministic
    assert 0.0 <= a["score"] <= 1.0                 # bounded
    # a clean strong setup scores higher than a weak/penalized one
    weak = compute_pulse_edge_score(**_score_inputs(
        tv_strength=0.1, cex_agreement=0.5, stale_class="not_stale", ob_imbalance=-0.3,
        spread=0.06, ask_depth_usd=1.0, ttc_s=10.0, realized_vol=1e-3))
    assert weak["score"] <= a["score"]
    assert 0.0 <= weak["score"] <= 1.0
    # all-missing inputs -> safe, bounded, bucket 'na'
    empty = compute_pulse_edge_score(
        tv_strength=None, cex_agreement=None, stale_class="insufficient_data", ob_imbalance=None,
        basket_direction=None, hurst_regime=None, spread=None, ask_depth_usd=None, ttc_s=None,
        realized_vol=None)
    assert empty["score"] == 0.0 and empty["bucket"] == "na"


def test_orderbook_pressure_imbalance():
    up = OrderBook(best_bid=0.50, best_ask=0.52, ask_depth_usd=1000.0, bid_depth_usd=3000.0,
                   asks=[(0.52, 10000.0)], bids=[(0.50, 10000.0)])
    obp = orderbook_pressure(up, None, size_usd=5.0)
    assert obp["bucket"] == "bid_heavy" and obp["imbalance"] == 0.5
    assert orderbook_pressure(None, None)["bucket"] == "na"


# ------------------------------- learner buckets reconcile + promotion --------------------- #
def test_edge_learner_buckets_reconcile_and_promotion():
    L = EdgeSignalLearner()
    for i in range(60):
        won = True
        L.record_settled({"stale_divergence": "stale_polymarket_up", "ttc_bucket": "240_300s",
                          "ob_pressure": "bid_heavy", "edge_score": "very_high",
                          "cex_agreement": "strong"},
                         won=won, pnl=2.0, ev_after_cost=0.05, reconciled=True)
    rep = L.report(promotion_allowed=False, min_samples=50)
    assert rep["settled"] == 60
    for dim in EdgeSignalLearner.DIMS:
        assert sum(b["n"] for b in rep["by_" + dim].values()) == 60, dim
    assert rep["promotion"]["any_eligible"] is True
    assert rep["promotion"]["promotion_allowed_by_config"] is False
    assert rep["best_buckets_after_cost"] and rep["best_buckets_after_cost"][0]["win_rate"] == 1.0
    # negative-EV bucket is NOT promotion-eligible
    L2 = EdgeSignalLearner()
    for _ in range(60):
        L2.record_settled({"edge_score": "high"}, won=True, pnl=2.0, ev_after_cost=-0.01,
                         reconciled=True)
    assert L2.report(min_samples=50)["promotion"]["any_eligible"] is False


# ============================ engine: reconcile + cannot bypass gate ======================= #
class _Mkt:
    def __init__(self, w, *, deep):
        self._w = w
        self._deep = deep

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        if self._deep:
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=50000,
                                  bid_depth_usd=50000, asks=[(0.55, 100000.0)],
                                  bids=[(0.50, 100000.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=49000,
                                    bid_depth_usd=44000, asks=[(0.49, 100000.0)],
                                    bids=[(0.44, 100000.0)])
        else:
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=2.0,
                                  bid_depth_usd=2.0, asks=[(0.55, 1.0)], bids=[(0.50, 1.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=2.0,
                                    bid_depth_usd=2.0, asks=[(0.49, 1.0)], bids=[(0.44, 1.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _engine(tmp_path, *, deep):
    t0 = 9_940_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                      edge_signal_enabled=True, data_dir=str(tmp_path))
    return PulseEngine(cfg, market_feed=_Mkt(win, deep=deep), price_feed=feed), t0


def test_engine_edge_report_fields_and_reconcile(tmp_path):
    eng, t0 = _engine(tmp_path, deep=True)
    # inject CEX prices so the basket has data (no network in CI)
    for dt in range(0, 40, 4):
        eng.edge_signal.basket.observe("binance_btcusdt", 64000.0 + dt, t0 - 40 + dt)
        eng.edge_signal.basket.observe("coinbase_btcusd", 63990.0 + dt, t0 - 40 + dt)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)                          # settle
    es = eng.status()["edge_signal"]
    # required report fields exist
    for fld in ("cex_basket_coverage", "by_stale_divergence", "by_ttc_bucket", "by_ob_pressure",
                "by_edge_score", "by_cex_agreement", "best_buckets_after_cost",
                "worst_buckets_after_cost", "promotion", "snapshots"):
        assert fld in es, fld
    # bucket stats reconcile with the ledger: settled count + each dimension total
    assert es["settled"] == eng.ledger.settled >= 1
    for dim in ("stale_divergence", "ttc_bucket", "ob_pressure", "edge_score", "cex_agreement"):
        assert sum(b["n"] for b in es["by_" + dim].values()) == es["settled"]
    # an accepted candidate carries the observe-only edge snapshot
    acc = [r for r in eng.status()["recent_evaluations"] if r["terminal"] == "accepted"]
    assert acc and acc[0]["edge"]["observe_only"] is True
    assert acc[0]["edge"]["affects_trading"] is False
    assert eng.light_report()["global_reconciled"] is True


def test_engine_edge_signal_cannot_bypass_execution_gate(tmp_path):
    eng, t0 = _engine(tmp_path, deep=False)         # thin book -> gate must reject
    for dt in range(0, 40, 4):                      # strong rising CEX momentum (high edge score)
        eng.edge_signal.basket.observe("binance_btcusdt", 64000.0 * (1 + 0.0002 * dt), t0 - 40 + dt)
        eng.edge_signal.basket.observe("coinbase_btcusd", 63990.0 * (1 + 0.0002 * dt), t0 - 40 + dt)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    assert eng.ledger.trades == 0                   # edge signal cannot force a fill past the gate
    assert eng.ledger.exec_gate_stats()["rejected"]["partial_fill_risk"] >= 1
    assert eng.status()["live_trading_enabled"] is False
