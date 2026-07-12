"""BTC 5-minute pulse paper engine — PAPER ONLY, loosened gates, no real orders.

Covers: window parsing, digital fair value, rolling vol, open-snapshot gating, the
loosened decision, paper fill + settlement P&L, calibration, and a full deterministic
engine tick (warm vol -> catch open -> trade -> settle -> calibrate)."""

from __future__ import annotations

import math

from engine.pulse.markets import PulseWindow, PulseMarketFeed, OrderBook
from engine.pulse.fair_value import digital_p_up, RollingVol
from engine.pulse.price import PulsePriceFeed
from engine.pulse.strategy import decide, PulseDecision
from engine.pulse.executor import PulseLedger
from engine.pulse.settlement import PulseCalibration, resolve_outcome
from engine.pulse.engine import PulseEngine, PulseConfig


# --- market parsing --------------------------------------------------------- #
def test_parse_window_from_gamma_event():
    ev = {"id": "613311", "slug": "btc-updown-5m-1781994000",
          "title": "Bitcoin Up or Down - June 20, 6:20PM-6:25PM ET",
          "endDate": "2026-06-20T22:25:00Z",
          "markets": [{"id": "2611559", "outcomes": '["Up","Down"]',
                       "clobTokenIds": '["UP_TOK","DOWN_TOK"]',
                       "endDate": "2026-06-20T22:25:00Z", "orderPriceMinTickSize": 0.01}]}
    w = PulseMarketFeed.parse_window(ev)
    assert w is not None
    assert w.up_token_id == "UP_TOK" and w.down_token_id == "DOWN_TOK"
    assert w.open_ts == 1781994000.0                 # from slug
    assert w.close_ts - w.open_ts == 300             # 5-min window
    assert w.event_id == "613311" and w.market_id == "2611559"


def test_parse_window_maps_outcomes_when_reordered():
    ev = {"id": "e", "slug": "btc-updown-5m-1000000000", "title": "t",
          "markets": [{"id": "m", "outcomes": '["Down","Up"]',
                       "clobTokenIds": '["DTOK","UTOK"]', "endDate": "2026-06-20T22:25:00Z"}]}
    w = PulseMarketFeed.parse_window(ev)
    assert w.up_token_id == "UTOK" and w.down_token_id == "DTOK"   # mapped by name, not order


# --- digital fair value ----------------------------------------------------- #
def test_digital_p_up_sign_bounds_and_collapse():
    sig = 1e-4
    up = digital_p_up(100_100, 100_000, sig, 120)     # above open -> >0.5
    flat = digital_p_up(100_000, 100_000, sig, 120)   # at open -> ~0.5
    down = digital_p_up(99_900, 100_000, sig, 120)    # below open -> <0.5
    assert down < flat < up
    assert abs(flat - 0.5) < 1e-3                    # ~0.5 (tiny Ito -0.5*sig^2 drift)
    # collapse near expiry: tiny remaining time -> near-certain
    assert digital_p_up(100_050, 100_000, sig, 0.01) > 0.99
    assert digital_p_up(100_000, 100_000, sig, 0) == 1.0      # r=0, tie -> Up
    assert digital_p_up(99_999, 100_000, sig, 0) == 0.0       # r=0, below -> Down
    assert digital_p_up(100_000, 100_000, 0.0, 100) is None   # no vol -> undefined


def test_rolling_vol_per_sec():
    rv = RollingVol(min_samples=8)
    assert rv.per_sec() is None
    base = 1000.0
    for i in range(40):
        base *= 1 + (0.0002 if i % 2 else -0.00015)
        rv.observe(base, now=1000.0 + i)
    v = rv.per_sec(now=1040.0)
    assert v is not None and v > 0


# --- open-snapshot gating --------------------------------------------------- #
def test_open_snapshot_captured_once_and_flags_late():
    seq = iter([100.0, 101.0, 102.0])
    f = PulsePriceFeed(fetcher=lambda: next(seq, 102.0), max_open_lag_s=10.0)
    f.poll(now=1000.0)                       # price 100 before open
    assert f.snapshot_open("w", open_ts=1005.0, now=1000.0) is None    # not open yet
    f.poll(now=1006.0)                       # price 101
    snap = f.snapshot_open("w", open_ts=1005.0, now=1006.0)
    assert snap is not None and snap.price == 101.0 and snap.lag_s == 1.0
    # second call returns the SAME snapshot (captured once)
    assert f.snapshot_open("w", open_ts=1005.0, now=2000.0).price == 101.0
    # a second window with the same boundary recovers the stored boundary observation rather than
    # substituting the current price seen 35 seconds later.
    f.poll(now=1040.0)
    late = f.snapshot_open("late", open_ts=1005.0, now=1040.0)
    assert late.price == 101.0 and late.lag_s == 1.0


# --- loosened decision ------------------------------------------------------ #
def _win(now0=1000.0):
    w = PulseWindow(event_id="e", market_id="m", slug="s", title="BTC Up or Down",
                    open_ts=now0, close_ts=now0 + 300, up_token_id="U", down_token_id="D")
    w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=500, bid_depth_usd=500)
    w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=500, bid_depth_usd=500)
    return w


def test_decide_picks_side_with_edge():
    w = _win()
    d = decide(w, fair_p_up=0.80, now=1100.0, min_edge=0.05, edge_buffer=0.01)
    assert d.trade and d.side == "up" and d.token_id == "U" and d.price == 0.55
    # fair 0.80 -> up edge 0.80-0.55-0.01=0.24 beats down edge 0.20-0.49-0.01
    assert abs(d.edge - 0.24) < 1e-9


def test_decide_force_side_down_evaluates_down_not_up():
    w = _win()
    d = decide(w, fair_p_up=0.80, now=1100.0, min_edge=0.05, edge_buffer=0.01, force_side="down")
    assert d.side == "down" and d.price == 0.49
    assert d.trade is False and d.reason == "edge_below_min"
    w2 = _win()
    w2.down_book = OrderBook(best_bid=0.10, best_ask=0.15, ask_depth_usd=500, bid_depth_usd=500)
    d2 = decide(w2, fair_p_up=0.30, now=1100.0, min_edge=0.05, edge_buffer=0.01, force_side="down")
    assert d2.trade and d2.side == "down"


def test_decide_rejects_low_edge_and_late_window():
    w = _win()
    assert decide(w, 0.57, 1100.0, min_edge=0.05).reason == "edge_below_min"
    assert decide(w, 0.99, 1299.0, min_seconds_to_close=4.0).reason == "too_close_to_settlement"
    w2 = _win()
    w2.up_book = OrderBook(best_bid=0.5, best_ask=None)
    w2.down_book = OrderBook(best_bid=0.5, best_ask=None)
    assert decide(w2, 0.99, 1100.0).reason == "no_tradeable_ask"


def test_decide_reward_risk_floor_skips_tiny_payoff_high_price():
    # high-price up entry: ask 0.91 -> win ~$0.49 per $5 vs full-$5 risk (reward/risk ~0.099)
    w = _win()
    w.up_book = OrderBook(best_bid=0.90, best_ask=0.91, ask_depth_usd=500, bid_depth_usd=500)
    w.down_book = OrderBook(best_bid=0.07, best_ask=0.09, ask_depth_usd=500, bid_depth_usd=500)
    # floor OFF -> the +EV (fair 0.98 > 0.91) high-price trade is taken
    d_off = decide(w, 0.98, 1100.0, min_edge=0.02, edge_buffer=0.01, min_reward_risk=0.0)
    assert d_off.trade and d_off.side == "up" and d_off.price == 0.91
    # floor ON (0.25 => need price <= ~0.80) -> skipped with the explicit reason
    d_on = decide(w, 0.98, 1100.0, min_edge=0.02, edge_buffer=0.01, min_reward_risk=0.25)
    assert d_on.trade is False and d_on.reason == "reward_risk_too_low"
    # a healthier-payoff entry (ask 0.55 -> reward/risk ~0.82) still passes the same floor
    w2 = _win()
    d2 = decide(w2, 0.80, 1100.0, min_edge=0.05, edge_buffer=0.01, min_reward_risk=0.25)
    assert d2.trade and d2.side == "up" and d2.price == 0.55
    # stricter UP-only floor rejects 0.72 (rr~0.39) while base 0.25 would allow it
    w3 = _win()
    w3.up_book = OrderBook(best_bid=0.70, best_ask=0.72, ask_depth_usd=500, bid_depth_usd=500)
    w3.down_book = OrderBook(best_bid=0.26, best_ask=0.28, ask_depth_usd=500, bid_depth_usd=500)
    d_up = decide(w3, 0.85, 1100.0, min_edge=0.05, edge_buffer=0.01,
                  min_reward_risk=0.25, min_reward_risk_up=0.45)
    assert d_up.trade is False and d_up.reason == "reward_risk_too_low"


def test_decide_quality_gates_early_window_and_basis_buffer():
    w = _win(1000.0)
    # too early in the window (move hasn't developed) -> skip
    assert decide(w, 0.99, 1010.0, min_seconds_since_open=30.0).reason == "too_early_in_window"
    # basis buffer raises the edge bar: up edge 0.80-0.55=0.25; buffer 0.01+0.20=0.21 -> 0.04 < min
    d = decide(w, 0.80, 1100.0, min_edge=0.05, edge_buffer=0.01, basis_buffer=0.20)
    assert d.reason == "edge_below_min" and abs(d.edge - 0.04) < 1e-9


# --- paper ledger + settlement ---------------------------------------------- #
def test_ledger_open_and_settle_win_and_loss():
    led = PulseLedger()
    w = _win()
    d = decide(w, 0.80, 1100.0, min_edge=0.05)
    pos = led.open_position(w, d, now=1100.0, size_usd=10.0, s_open=64000.0)
    assert pos is not None and pos.shares == round(10 / 0.55, 6)
    assert not led.open_position(w, d, now=1101.0, size_usd=10.0)   # one per window
    led.settle("e", outcome_up=True, s_open=64000.0, s_close=64100.0)
    assert pos.won is True and pos.pnl_usd > 0 and led.realized_pnl > 0
    # a losing position
    led2 = PulseLedger()
    w2 = _win(2000.0)
    d2 = decide(w2, 0.80, 2100.0, min_edge=0.05)
    led2.open_position(w2, d2, now=2100.0, size_usd=10.0)
    led2.settle("e", outcome_up=False)
    assert led2.positions["e"].won is False and led2.positions["e"].pnl_usd == -10.0


def test_ledger_subtracts_entry_fee_from_realized_pnl():
    led = PulseLedger()
    w = _win()
    d = decide(w, 0.80, 1100.0, min_edge=0.05)
    pos = led.open_position(w, d, now=1100.0, size_usd=10.0)
    pos.entry_fee_usd = 0.25
    led.settle("e", outcome_up=True)
    gross = pos.shares - pos.size_usd
    assert pos.pnl_usd == round(gross - 0.25, 6)


def test_default_settlement_policy_requires_official_resolution():
    assert PulseConfig().settlement_source_priority == ("polymarket_resolution",)


def test_calibration_brier():
    cal = PulseCalibration()
    for _ in range(10):
        cal.observe(0.9, True)
    cal.observe(0.9, False)
    assert cal.n == 11 and 0 < cal.brier < 0.25
    assert cal.base_rate_up == round(10 / 11, 4)


def test_resolve_outcome_prefers_polymarket_then_proxy():
    class G:
        def __init__(self, r): self.r = r
        def fetch_resolution(self, mid): return self.r
    assert resolve_outcome("m", gamma_feed=G(True))[0] is True
    assert resolve_outcome("m", gamma_feed=G(True))[1] == "polymarket"
    # gamma not ready -> proxy when allowed
    o, src = resolve_outcome("m", gamma_feed=G(None), s_open=100.0, s_close=101.0)
    assert o is True and src == "proxy_coinbase"
    # gamma not ready + proxy disallowed -> unresolved
    assert resolve_outcome("m", gamma_feed=G(None), s_open=100.0, s_close=101.0,
                           allow_proxy=False)[0] is None


# --- full engine tick (deterministic) --------------------------------------- #
class _FakeMarket:
    def __init__(self, window, resolution):
        self.window = window
        self.resolution = resolution
    def active_windows(self, now=None, **kw):
        return [self.window]
    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=550, bid_depth_usd=500,
                              asks=[(0.55, 1000.0)], bids=[(0.50, 1000.0)])
        w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=490, bid_depth_usd=440,
                                asks=[(0.49, 1000.0)], bids=[(0.44, 1000.0)])
        return w
    def fetch_resolution(self, market_id):
        return self.resolution


class _StubTradingView:
    """Minimal TV stub so baseline UP gate sees UP_STRONG during integration ticks."""

    def drain_pending(self):
        return []

    def latest_feature(self, **kw):
        return {
            "direction": "UP",
            "strength": 0.85,
            "signal_level": "UP_STRONG",
            "age_s": 1.0,
            "mtf_alignment": "bullish_aligned",
        }

    def report(self):
        return {
            "enabled": True,
            "tradingview_observe_only": True,
            "tradingview_alerts_received": 1,
            "tradingview_alerts_valid": 1,
            "tradingview_alerts_rejected": 0,
            "tradingview_reject_reasons": {},
            "tradingview_latest_signal": self.latest_feature(),
        }


def test_engine_full_cycle_trade_and_settle(tmp_path):
    t0 = 1_000_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="btc-updown-5m-1000000",
                      title="Bitcoin Up or Down", open_ts=t0, close_ts=t0 + 300,
                      up_token_id="U", down_token_id="D")
    # rising price -> P(up) high -> buy Up cheap (ask 0.55); resolves Up -> win
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]

    feed = PulsePriceFeed(fetcher=fetch, vol=RollingVol(window_s=900, min_samples=8),
                          max_open_lag_s=20.0)
    eng = PulseEngine(PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02,
                                  edge_buffer=0.01, basis_buffer=0.0,
                                  min_seconds_since_open=0.0, sigma_trust_floor=0.0,
                                  min_vol_samples=2,
                      tv_mtf_conflict_gate_enabled=False,
                                  tv_down_bias_gate_enabled=False,
                                  directional_down_only=False,
                                  directional_block_up_until_promoted=False,
                                  directional_up_restrictions_enabled=False,
                                  data_dir=str(tmp_path)),
                      market_feed=_FakeMarket(win, resolution=True), price_feed=feed)
    eng.tradingview = _StubTradingView()
    for i in range(12):                      # warm vol BEFORE the window opens
        eng.tick(now=t0 - 12 + i)
    assert eng.ledger.trades == 0            # window not open yet
    eng.tick(now=t0 + 2)                      # open captured (lag 2s); s_now==s_open, no trade
    assert eng.ledger.trades == 0
    for k in range(1, 8):                     # price keeps rising above open -> P(up) climbs
        eng.tick(now=t0 + 2 + k * 5)
    assert eng.ledger.trades == 1
    pos = eng.ledger.positions["e1"]
    assert pos.side == "up" and pos.s_open is not None
    eng.tick(now=t0 + 301)                    # past close -> settle (gamma says Up)
    assert eng.ledger.settled == 1 and pos.won is True and pos.pnl_usd > 0
    assert eng.calib.n == 1
    # resolved via official Polymarket resolution (not the RTDS Chainlink proxy)
    assert eng.ledger.settle_sources["polymarket_resolution"] == 1
    assert eng.ledger.stats()["settle_sources"]["rtds_chainlink_proxy"] == 0
    assert eng.status()["paper_only"] is True and eng.status()["live_trading_enabled"] is False
    # status + ledger persisted
    assert (tmp_path / "btc_pulse_status.json").exists()
    assert (tmp_path / "btc_pulse_ledger.json").exists()


def test_engine_skips_untrusted_flat_vol(tmp_path):
    # a perfectly flat price -> sigma floored -> below trust floor -> never trades (noise guard)
    t0 = 3_000_000.0
    win = PulseWindow(event_id="ef", market_id="mf", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    feed = PulsePriceFeed(fetcher=lambda: 64000.0, vol=RollingVol(min_samples=5),
                          max_open_lag_s=30.0)
    eng = PulseEngine(PulseConfig(data_dir=str(tmp_path), min_seconds_since_open=0.0,
                                  sigma_trust_floor=2.0e-6, min_vol_samples=5),
                      market_feed=_FakeMarket(win, resolution=True), price_feed=feed)
    for i in range(15):
        eng.tick(now=t0 + i)                 # in-window, flat price
    assert eng.ledger.trades == 0
    assert eng.reconciler.report()["skipped_by_reason"].get("untrusted_vol", 0) >= 1


def test_profit_metrics_edge_realized_and_per_side(tmp_path):
    from engine.pulse.executor import PulseLedger
    led = PulseLedger()

    def _add(key, side, entry, won):
        w = _win()
        w.event_id = key
        d = PulseDecision(True, side=side, token_id="T", price=entry, fair_p_up=0.6, edge=0.1)
        led.open_position(w, d, now=1.0, size_usd=10.0)
        led.settle(key, outcome_up=(won if side == "up" else not won))
    _add("a", "up", 0.40, True)
    _add("b", "up", 0.45, False)
    _add("c", "down", 0.50, True)
    s = led.stats()
    assert s["settled"] == 3 and s["wins"] == 2
    assert s["avg_entry_price"] == round((0.40 + 0.45 + 0.50) / 3, 4)
    assert s["edge_realized"] == round(2 / 3 - 0.45, 4)      # win_rate - avg cost (profit signal)
    assert s["win_rate_up"] == 0.5 and s["win_rate_down"] == 1.0
    assert s["side_counts"] == {"up": 2, "down": 1}


def test_pulse_state_persists_and_reloads_across_restart(tmp_path):
    from engine.pulse.executor import PulsePosition
    cfg = PulseConfig(data_dir=str(tmp_path))
    eng = PulseEngine(cfg, market_feed=_FakeMarket(None, True),
                      price_feed=PulsePriceFeed(fetcher=lambda: 64000.0))
    eng.ledger.trades, eng.ledger.settled, eng.ledger.wins = 3, 2, 1
    eng.ledger.realized_pnl = 8.18
    eng.ledger.positions["w1"] = PulsePosition(
        window_key="w1", market_id="m", title="t", side="up", token_id="U",
        entry_price=0.55, size_usd=10.0, shares=18.18, fair_at_entry=0.8,
        edge_at_entry=0.24, open_ts=1.0, close_ts=301.0, entry_ts=2.0,
        status="settled", outcome_up=True, won=True, pnl_usd=8.18)
    eng.calib.observe(0.8, True)
    eng.calib.observe(0.3, False)
    eng._persist()
    # "restart": a new engine on the same data dir restores everything
    eng2 = PulseEngine(PulseConfig(data_dir=str(tmp_path)),
                       market_feed=_FakeMarket(None, True),
                       price_feed=PulsePriceFeed(fetcher=lambda: 64000.0))
    assert eng2.ledger.trades == 3 and eng2.ledger.settled == 2 and eng2.ledger.wins == 1
    assert abs(eng2.ledger.realized_pnl - 8.18) < 1e-6
    assert "w1" in eng2.ledger.positions
    assert eng2.calib.n == 2                     # calibration accumulators restored exactly


def test_fresh_start_archives_prior_ledger(tmp_path):
    import glob
    (tmp_path / "btc_pulse_ledger.json").write_text(
        '{"stats": {"trades": 9, "settled": 9, "wins": 5, "realized_pnl_usd": 12.0}, '
        '"positions": [], "calibration_state": {"n": 9, "sq": 1.0, "ll": 1.0, "up_outcomes": 5}}')
    eng = PulseEngine(PulseConfig(data_dir=str(tmp_path), fresh_start=True),
                      market_feed=_FakeMarket(None, True),
                      price_feed=PulsePriceFeed(fetcher=lambda: 64000.0))
    assert eng.ledger.trades == 0 and eng.calib.n == 0          # clean baseline
    assert glob.glob(str(tmp_path / "btc_pulse_ledger.archived_*.json"))   # prior archived


def test_grok_overlay_sanitize_clamp_and_fail_open():
    from engine.pulse.overlay import GrokEventOverlay
    # vol_multiplier can only ever be >=1 (more cautious) and capped
    ov = GrokEventOverlay(assessor=lambda: {"regime": "event_risk", "vol_multiplier": 0.4,
                                            "blackout": True, "reason": "CPI"},
                          vol_mult_cap=3.0)
    s = ov.refresh(now=1000.0)
    assert s["blackout"] is True and s["vol_multiplier"] == 1.0      # 0.4 clamped up to 1.0
    ov2 = GrokEventOverlay(assessor=lambda: {"vol_multiplier": 9.0, "blackout": False},
                           vol_mult_cap=3.0)
    assert ov2.refresh(now=1000.0)["vol_multiplier"] == 3.0          # clamped to cap
    # fail-open: a None/raising assessor -> neutral
    ovn = GrokEventOverlay(assessor=lambda: None)
    ovn.refresh(now=1000.0)
    assert ovn.current(now=1000.0)["blackout"] is False
    # stale state -> neutral
    ov.refresh(now=1000.0)
    assert ov.current(now=1000.0 + 10_000)["regime"] == "unknown"


def test_grok_overlay_rate_limit():
    from engine.pulse.overlay import GrokEventOverlay
    calls = {"n": 0}

    def _a():
        calls["n"] += 1
        return {"regime": "calm", "vol_multiplier": 1.0, "blackout": False}
    ov = GrokEventOverlay(assessor=_a, max_calls_per_hour=2)
    for i in range(5):
        ov.refresh(now=1000.0 + i)
    assert calls["n"] == 2                                          # capped at 2/hour


class _StubOverlay:
    def __init__(self, state):
        self._state = state
    def current(self, now=None):
        return self._state


def test_engine_blackout_skips_opens(tmp_path):
    t0 = 4_500_000.0
    win = PulseWindow(event_id="eb", market_id="mb", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, vol=RollingVol(window_s=900, min_samples=8),
                          max_open_lag_s=20.0)
    eng = PulseEngine(PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02,
                                  min_seconds_since_open=0.0, sigma_trust_floor=0.0,
                                  min_vol_samples=2, basis_buffer=0.0, data_dir=str(tmp_path)),
                      market_feed=_FakeMarket(win, resolution=True), price_feed=feed)
    eng.overlay = _StubOverlay({"blackout": True, "vol_multiplier": 1.0, "ts": t0})
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(1, 8):
        eng.tick(now=t0 + 2 + k * 5)
    assert eng.ledger.trades == 0                                   # blackout blocked all opens
    assert eng.reconciler.report()["skipped_by_reason"].get("grok_event_blackout", 0) >= 1


def test_engine_skips_window_with_late_open(tmp_path):
    # joining mid-window (open seen >max_open_lag late) -> never trades that window
    t0 = 2_000_000.0
    win = PulseWindow(event_id="e2", market_id="m2", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    feed = PulsePriceFeed(fetcher=lambda: 64000.0, vol=RollingVol(min_samples=8),
                          max_open_lag_s=20.0)
    eng = PulseEngine(PulseConfig(data_dir=str(tmp_path)),
                      market_feed=_FakeMarket(win, resolution=True), price_feed=feed)
    for i in range(12):
        eng.tick(now=t0 + 100 + i)            # first sight is 100s into the window
    assert eng.ledger.trades == 0
    assert eng.reconciler.report()["skipped_by_reason"].get("open_snapshot_late", 0) >= 1


def test_reward_risk_floor_up_premium():
    eng = PulseEngine(PulseConfig(min_reward_risk=0.40, min_reward_risk_up_premium=0.15))
    assert eng._reward_risk_floor("down") == 0.40
    assert eng._reward_risk_floor("up") == 0.55
    assert eng._ask_reward_risk_ok("up", 0.72) is False
    assert eng._ask_reward_risk_ok("up", 0.55) is True


def test_grok_up_side_blocked_at_coin_flip_accuracy():
    eng = PulseEngine(PulseConfig())
    eng.grok_decider = type("G", (), {"report": lambda self: {
        "graded_directional": 42, "direction_accuracy": 0.5}})()
    assert eng._grok_up_side_allowed() is False


def test_baseline_up_tv_strength_gate():
    eng = PulseEngine(PulseConfig(baseline_up_tv_gate_enabled=True))
    ok, reason = eng._baseline_up_tv_strength_ok({"direction": "DOWN", "strength": 0.5})
    assert ok is False and reason == "baseline_up_tv_opposes"
    ok, reason = eng._baseline_up_tv_strength_ok({"direction": "UP", "strength": 0.65})
    assert ok is False and reason == "baseline_up_tv_weak"
    ok, reason = eng._baseline_up_tv_strength_ok(
        {"direction": "UP", "strength": 0.85, "signal_level": "UP_WEAK"})
    assert ok is False and reason == "baseline_up_tv_not_strong"
    ok, _ = eng._baseline_up_tv_strength_ok(
        {"direction": "UP", "strength": 0.85, "signal_level": "UP_STRONG"})
    assert ok is True
    ok, reason = eng._baseline_up_tv_strength_ok(None)
    assert ok is False and reason == "baseline_up_tv_missing"
