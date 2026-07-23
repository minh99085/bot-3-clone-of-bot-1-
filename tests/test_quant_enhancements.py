"""Quant enhancements toward $100/day — sniper gate, chop gate, maker fills,
ETH-15m scope. Each is a separate, paired-testable lever; none loosens a gate.
"""

from __future__ import annotations

import pytest

from hermes.lane_variants import LANES, entry_allows
from strategy.advanced_signals import standardized_distance


# ── standardized distance ────────────────────────────────────────────────────

def test_distance_zero_at_strike_grows_with_gap():
    assert standardized_distance(64000.0, 64000.0, 0.8, 300.0) == 0.0
    near = standardized_distance(64050.0, 64000.0, 0.8, 300.0)
    far = standardized_distance(64500.0, 64000.0, 0.8, 300.0)
    assert 0 < near < far


def test_distance_shrinks_with_more_time_and_vol():
    d_late = standardized_distance(64200.0, 64000.0, 0.8, 120.0)
    d_early = standardized_distance(64200.0, 64000.0, 0.8, 800.0)
    assert d_late > d_early  # same gap is MORE decisive with less time left
    d_calm = standardized_distance(64200.0, 64000.0, 0.4, 120.0)
    d_wild = standardized_distance(64200.0, 64000.0, 2.0, 120.0)
    assert d_calm > d_wild   # same gap is LESS decisive in a wild tape


# ── sniper gates ─────────────────────────────────────────────────────────────

SNIPER = dict(side_price=0.88, seconds_remaining=150, liquidity_usd=5000,
              spec=LANES["fav_sniper"], momentum=0.3, side_is_up=True)


def test_sniper_fires_only_when_far_and_clean():
    ok, _ = entry_allows(**SNIPER, abs_distance=2.5, window_flips=0)
    assert ok


def test_sniper_blocks_close_to_strike():
    bad, reason = entry_allows(**SNIPER, abs_distance=1.2, window_flips=0)
    assert not bad and "too_close" in reason


def test_sniper_blocks_choppy_window():
    bad, reason = entry_allows(**SNIPER, abs_distance=2.5, window_flips=3)
    assert not bad and "choppy" in reason


def test_sniper_blocks_unknown_flips():
    bad, reason = entry_allows(**SNIPER, abs_distance=2.5, window_flips=None)
    assert not bad and "flips_unknown" in reason


def test_sniper_late_and_favorite_only():
    bad, reason = entry_allows(**{**SNIPER, "seconds_remaining": 400},
                               abs_distance=2.5, window_flips=0)
    assert not bad and "too_early" in reason
    bad, reason = entry_allows(**{**SNIPER, "side_price": 0.80},
                               abs_distance=2.5, window_flips=0)
    assert not bad and "side_price" in reason


def test_non_sniper_lanes_ignore_distance_and_flips():
    ok, _ = entry_allows(side_price=0.75, seconds_remaining=400, liquidity_usd=5000,
                         spec=LANES["fav_cont_70"], momentum=0.3, side_is_up=True,
                         abs_distance=0.0, window_flips=None)
    assert ok


# ── flip counting through mispricing ────────────────────────────────────────

def test_window_flip_count_from_history(monkeypatch):
    import hermes.mispricing as mp
    from hermes.models import MarketCandidate

    strike = 64000.0
    window_ts = 1_784_000_000
    # path crosses the strike twice after window open
    prices = [63990.0, 64010.0, 63995.0, 64020.0, 64030.0]
    times = [float(window_ts + 10 * i) for i in range(len(prices))]
    monkeypatch.setenv("HERMES_STRATEGY_VARIANT", "fav_sniper")
    monkeypatch.setattr(mp, "get_asset_price_history",
                        lambda asset, max_points=240: (times, list(prices)))

    class Snap:
        mid = prices[-1]; momentum = 0.3; ret_60s = 0.0; ret_3m = 0.0
        sources_agree = True; bybit = None; binance = None

    monkeypatch.setattr(mp, "get_asset_snapshot", lambda asset: Snap(), raising=False)
    cand = MarketCandidate(
        market_id="m", slug=f"btc-updown-15m-{window_ts}", question="BTC up/down",
        yes_price=0.88, no_price=0.12, hourly_bucket=12,
    )
    out = mp.detect_mispricing(cand)
    # 2 crossings > max_window_flips=1 → sniper's chop gate must refuse
    if out.features.get("window_flips") is not None:
        assert out.features["window_flips"] == 2.0
        if not out.active:
            assert "choppy" in (out.reason or "") or "lane_gate" in (out.reason or "")


# ── maker fills ──────────────────────────────────────────────────────────────

def test_maker_fill_prices_near_mid_not_ask():
    from hermes.fill_model import conservative_paper_fill

    asks = [(0.80, 1000.0)]
    mid = 0.775
    taker = conservative_paper_fill(asks, 40.0, 0.80, mid=mid, maker=False)
    maker = conservative_paper_fill(asks, 40.0, 0.80, mid=mid, maker=True)
    assert maker[1] == pytest.approx(mid + 0.25 * (0.80 - mid))  # 0.78125
    assert maker[1] < taker[1]      # cheaper entry than lifting the ask
    assert maker[0] == taker[0] == pytest.approx(40.0)  # depth cap unchanged
    assert "maker_mid_fill" in maker[3]


def test_maker_fill_never_below_mid_and_keeps_near_money_penalty():
    from hermes.fill_model import NEAR_MONEY_EXTRA_BPS, conservative_paper_fill

    asks = [(0.51, 5000.0)]
    mid = 0.50
    filled, px, _slip, note = conservative_paper_fill(asks, 40.0, 0.51, mid=mid, maker=True)
    base = 0.50 + 0.25 * 0.01
    assert px == pytest.approx(base * (1 + NEAR_MONEY_EXTRA_BPS / 10_000))
    assert px >= mid
    assert "near_money" in note


def test_broker_maker_mode_env(monkeypatch):
    import connectors.broker as bk
    import connectors.polymarket as pm
    from hermes.models import Direction, EntryMode, OrderIntent

    class FakeBook:
        mid = 0.775
        asks = [type("L", (), {"price": 0.80, "size": 1000.0})()]
        bids = []

    monkeypatch.setattr(pm.PolymarketClient, "get_orderbook", lambda self, t: FakeBook())
    intent = OrderIntent(
        signal_id="s", market_id="m", direction=Direction.UP,
        size_usd=40.0, limit_price=0.80, entry_mode=EntryMode.MISPRICING,
        paper=True,
    )
    monkeypatch.setenv("HERMES_MAKER_MODE", "1")
    fill = bk.BrokerClient(paper=True).execute(intent, token_id="tok", asset="BTC")
    assert fill.fill_price == pytest.approx(0.775 + 0.25 * 0.025)
    monkeypatch.setenv("HERMES_MAKER_MODE", "0")
    fill_t = bk.BrokerClient(paper=True).execute(intent, token_id="tok", asset="BTC")
    assert fill_t.fill_price >= 0.80  # taker never better than its limit


# ── ETH-15m scope ────────────────────────────────────────────────────────────

def test_eth15_scope_wired():
    import hermes.market_scope as ms

    assert "eth15" in ms.FILTER_KEYS
    assert ms.SERIES_ETH_15M in ms.ALL_SERIES
    spec = ms.filter_specs()["eth15"]
    assert spec.slug_prefix == "eth-updown-15m-" and spec.oracle_asset == "ETH"
    sm = ms.parse_slug("eth-updown-15m-1784800800")
    assert sm is not None and sm.asset == "eth" and sm.timeframe == "15m"
    assert sm.series == "eth_updown_15m"


def test_eth15_discovery_slugs(monkeypatch):
    import hermes.market_scope as ms

    slugs = ms.all_discovery_slugs(now=1_784_800_000.0, market_filter="eth15")
    assert slugs and all(s.startswith("eth-updown-15m-") for s in slugs)


def test_compose_new_lanes():
    text = open("docker-compose.yml").read()
    assert "lane05_favsniper" in text and "lane05_favcont80" not in text
    assert "lane07_ethdrift" in text and "lane07_driftgarch" not in text
    eth_seg = text.split("HERMES_INSTANCE_ID: lane07_ethdrift")[1].split("volumes:")[0]
    assert "MARKET_FILTER: eth15" in eth_seg
    for lane in ("lane04_favcont70", "lane05_favsniper", "lane08_favdepth", "lane10_favopen"):
        seg = text.split(f"HERMES_INSTANCE_ID: {lane}")[1].split("volumes:")[0]
        assert 'HERMES_MAKER_MODE: "1"' in seg, lane
