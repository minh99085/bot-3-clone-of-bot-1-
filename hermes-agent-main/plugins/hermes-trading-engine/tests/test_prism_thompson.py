"""Tests for PRISM Phase 5 — Thompson bucket posteriors (PAPER ONLY)."""

import random

from engine.pulse.prism.thompson import (
    BucketKey,
    ThompsonStore,
    minute_band_from_seconds,
)


def _key(asset="btc", band="10-15m", regime="trending", tv="single"):
    return BucketKey(asset, band, regime, tv)


def test_minute_band_mapping():
    assert minute_band_from_seconds(30) == "0-5m"
    assert minute_band_from_seconds(8 * 60) == "5-10m"
    assert minute_band_from_seconds(12 * 60) == "10-15m"
    assert minute_band_from_seconds(25 * 60) == "other"     # gap band
    assert minute_band_from_seconds(48 * 60) == "45-50m"
    assert minute_band_from_seconds(None) == "other"


def test_new_bucket_is_probe_only():
    st = ThompsonStore()
    b = _key()
    assert st.probe_only(b) is True
    assert st.sniper_allowed(b) is False


def test_ten_wins_three_losses_sniper_allowed():
    st = ThompsonStore()
    b = _key()
    for _ in range(10):
        st.record(b, won=True, pnl=1.0, save=False)
    for _ in range(3):
        st.record(b, won=False, pnl=-1.0, save=False)
    # n=13 < 15 -> not yet; add 2 more wins to cross the sample floor with a high win rate
    st.record(b, won=True, pnl=1.0, save=False)
    st.record(b, won=True, pnl=1.0, save=False)
    assert st.probe_only(b) is False
    assert st.sniper_allowed(b) is True                     # expected_p ~0.75 > 0.55


def test_toxic_bucket_blocked_after_enough_samples():
    st = ThompsonStore()
    b = _key(asset="xrp", regime="chop_noise")
    for _ in range(4):
        st.record(b, won=True, pnl=1.0, save=False)
    for _ in range(21):
        st.record(b, won=False, pnl=-1.0, save=False)       # ~16% win rate, n=25
    assert st.block_bucket(b) is True


def test_healthy_bucket_not_blocked():
    st = ThompsonStore()
    b = _key()
    for _ in range(15):
        st.record(b, won=True, pnl=1.0, save=False)
    for _ in range(10):
        st.record(b, won=False, pnl=-1.0, save=False)
    assert st.block_bucket(b) is False


def test_bnb_pessimistic_prior_and_hard_block():
    st_default = ThompsonStore()
    st_block = ThompsonStore(bnb_block=True)
    bnb = _key(asset="bnb")
    # pessimistic prior alpha=2,beta=5 -> expected_p < 0.5 before any data
    assert st_default.expected_p_win(bnb) < 0.5
    # hard block only when configured
    assert st_default.block_bucket(bnb) is False
    assert st_block.block_bucket(bnb) is True


def test_sample_p_win_in_unit_interval():
    st = ThompsonStore(rng=random.Random(42))
    b = _key()
    for _ in range(20):
        v = st.sample_p_win(b)
        assert 0.0 <= v <= 1.0


def test_size_multiplier_clamped():
    st = ThompsonStore()
    b = _key()
    assert st.size_multiplier(b, ask=None) == 0.0
    assert st.size_multiplier(b, ask=0.5, sample=0.75) == (0.75 - 0.5) / (1 - 0.5)
    assert st.size_multiplier(b, ask=0.5, sample=0.40) == 0.0    # sample < ask -> 0
    assert st.size_multiplier(b, ask=0.5, sample=2.0) == 1.0     # clamped to 1


def test_confidence_factor_probe_and_proven():
    st = ThompsonStore()
    b = _key()
    assert st.thompson_confidence_factor(b) == 0.5              # probe
    for _ in range(10):
        st.record(b, won=True, pnl=1.0, save=False)
    f = st.thompson_confidence_factor(b)
    assert 0.3 <= f <= 1.0 and f > 0.5                         # proven high win rate


def test_key_from_trade_research_dict():
    st = ThompsonStore()
    b = st.key_from_trade({"series_label": "btc_1h", "markov_state": "trending",
                           "seconds_since_open_at_entry": 12 * 60})
    assert b.asset == "btc" and b.minute_band == "10-15m" and b.regime == "trending"


def test_persistence_roundtrip(tmp_path):
    st = ThompsonStore(data_dir=tmp_path)
    b = _key()
    for _ in range(6):
        st.record(b, won=True, pnl=1.5)                        # save=True writes json
    assert (tmp_path / "prism_thompson.json").exists()

    st2 = ThompsonStore(data_dir=tmp_path)
    post = st2.get(b)
    assert post.n == 6 and post.wins == 6
    assert abs(post.pnl_usd - 9.0) < 1e-9


def test_report_shape():
    st = ThompsonStore()
    st.record(_key(), won=True, pnl=1.0, save=False)
    rep = st.report()
    assert rep["enabled"] is True and rep["n_buckets"] >= 1
    assert isinstance(rep["top_buckets"], list)


# --------------------------------------------------------------------------------------------- #
# Engine integration: record on settle writes prism_thompson.json; BNB block-gate rejects entry.
# --------------------------------------------------------------------------------------------- #

def test_engine_records_thompson_on_settle(tmp_path):
    # Reuse the proven full-cycle harness (buys Up, resolves Up -> settled win) + prism_enabled.
    from engine.pulse.engine import PulseConfig, PulseEngine
    from engine.pulse.fair_value import RollingVol
    from engine.pulse.markets import PulseWindow
    from engine.pulse.price import PulsePriceFeed
    from tests.test_btc_pulse_engine import _FakeMarket, _StubTradingView

    t0 = 4_000_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="btc-updown-5m-4000000",
                      title="Bitcoin Up or Down", open_ts=t0, close_ts=t0 + 300,
                      up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]

    feed = PulsePriceFeed(fetcher=fetch, vol=RollingVol(window_s=900, min_samples=8),
                          max_open_lag_s=20.0)
    eng = PulseEngine(
        PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, edge_buffer=0.01,
                    basis_buffer=0.0, min_seconds_since_open=0.0, sigma_trust_floor=0.0,
                    min_vol_samples=2, tv_mtf_conflict_gate_enabled=False,
                    tv_down_bias_gate_enabled=False, directional_down_only=False,
                    directional_block_up_until_promoted=False,
                    directional_up_restrictions_enabled=False,
                    prism_enabled=True, data_dir=str(tmp_path)),
        market_feed=_FakeMarket(win, resolution=True), price_feed=feed)
    eng.tradingview = _StubTradingView()
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    eng.tick(now=t0 + 2)
    for k in range(1, 8):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 301)                        # past close -> settle

    assert eng.ledger.settled >= 1
    assert (tmp_path / "prism_thompson.json").exists()
    assert eng.prism_thompson.report()["n_buckets"] >= 1
    assert eng.status()["prism_thompson"]["enabled"] is True


def test_engine_bnb_block_rejects_at_prism_thompson(tmp_path):
    from engine.pulse.engine import PulseConfig, PulseEngine
    from engine.pulse.fair_value import RollingVol
    from engine.pulse.markets import PulseWindow, OrderBook
    from engine.pulse.price import PulsePriceFeed

    t0 = 5_000_000.0
    # a BNB hourly window (series_label -> asset "bnb")
    win = PulseWindow(event_id="e1", market_id="m1", slug="bnb-up-or-down-hourly-5000000",
                      title="BNB Up or Down", open_ts=t0, close_ts=t0 + 3600,
                      up_token_id="U", down_token_id="D", window_seconds=3600,
                      series_slug="bnb-up-or-down-hourly", series_label="bnb_1h")
    price = {"p": 600.0}

    def fetch():
        price["p"] += 0.2
        return price["p"]

    class _Mkt:
        def active_windows(self, now=None, **kw):
            return [win]
        def hydrate_books(self, w):
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=550, bid_depth_usd=500,
                                  bids=[(0.50, 900)], asks=[(0.55, 900)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=550,
                                    bid_depth_usd=500, bids=[(0.44, 900)], asks=[(0.49, 900)])
        def resolve_up(self, *a, **k):
            return None

    feed = PulsePriceFeed(fetcher=fetch, vol=RollingVol(window_s=900, min_samples=8),
                          max_open_lag_s=600.0)
    eng = PulseEngine(
        PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.01, edge_buffer=0.0,
                    basis_buffer=0.0, min_seconds_since_open=0.0, sigma_trust_floor=0.0,
                    min_vol_samples=2, directional_down_only=True,
                    prism_enabled=True, prism_thompson_gate_enabled=True, prism_bnb_block=True,
                    data_dir=str(tmp_path), fresh_start=True),
        market_feed=_Mkt(), price_feed=feed)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    eng.tick(now=t0 + 40)

    rbs = eng.reconciler.report()["rejected_by_stage"]
    assert rbs.get("prism_thompson", 0) >= 1
    assert eng.ledger.trades == 0
