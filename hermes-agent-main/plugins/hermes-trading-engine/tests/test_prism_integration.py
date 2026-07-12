"""PRISM Phase 6 — end-to-end engine integration (PAPER ONLY).

Drives a directional candidate through the full PRISM chain on the LEGACY path with mock books and
verifies: (1) the status API exposes the consolidated prism block with I/E/C/R + agent; (2) the
agent gate rejects a NO-agent candidate at stage prism_agent; (3) an accepted Sniper candidate is
sized by the PRISM allocator (> the $5 base) rather than the flat base size.
"""

from engine.pulse.engine import PulseConfig, PulseEngine
from engine.pulse.fair_value import RollingVol
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed


def _feed(step=3.0, start=64000.0):
    price = {"p": start}

    def fetch():
        price["p"] += step
        return price["p"]

    return PulsePriceFeed(fetcher=fetch, vol=RollingVol(window_s=900, min_samples=8),
                          max_open_lag_s=600.0)


class _Mkt:
    def __init__(self, win, up_ask=0.55, down_ask=0.49, resolution=None):
        self.win = win
        self.up_ask = up_ask
        self.down_ask = down_ask
        self.resolution = resolution

    def active_windows(self, now=None, **kw):
        return [self.win]

    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=self.up_ask - 0.05, best_ask=self.up_ask, ask_depth_usd=800,
                              bid_depth_usd=800, bids=[(self.up_ask - 0.05, 1500)],
                              asks=[(self.up_ask, 1500)])
        w.down_book = OrderBook(best_bid=self.down_ask - 0.05, best_ask=self.down_ask,
                                ask_depth_usd=800, bid_depth_usd=800,
                                bids=[(self.down_ask - 0.05, 1500)], asks=[(self.down_ask, 1500)])

    def resolve_up(self, *a, **k):
        return self.resolution


def _mk_window(t0):
    return PulseWindow(event_id="e1", market_id="m1", slug="btc-up-or-down-hourly",
                       title="Bitcoin Up or Down", open_ts=t0, close_ts=t0 + 3600,
                       up_token_id="U", down_token_id="D", window_seconds=3600,
                       series_slug="btc-up-or-down-hourly", series_label="btc_1h")


def test_status_exposes_prism_block(tmp_path):
    t0 = 6_000_000.0
    win = _mk_window(t0)
    eng = PulseEngine(
        PulseConfig(tick_seconds=1.0, size_usd=5.0, min_seconds_since_open=0.0,
                    sigma_trust_floor=0.0, min_vol_samples=2, directional_down_only=True,
                    prism_enabled=True, prism_cross_asset_enabled=True,
                    data_dir=str(tmp_path), fresh_start=True),
        market_feed=_Mkt(win), price_feed=_feed())
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    eng.prism_info.observe("chainlink_anchor", t0 + 40, t0 + 40)
    eng.tick(now=t0 + 40)

    prism = eng.status()["prism"]
    assert prism["enabled"] is True
    assert prism["trade_authority"] is False            # observe-only by default
    ens = prism["ensemble"]
    for k in ("E", "C", "C_final", "I", "R", "agent"):
        assert k in ens
    assert eng.status()["prism_agents"]["enabled"] is True


def test_agent_gate_rejects_no_agent(tmp_path):
    """With the agent gate ON and asks so far from fair that E<=0 (R->0), the candidate has no
    agent and is rejected at stage prism_agent."""
    t0 = 6_100_000.0
    win = _mk_window(t0)
    eng = PulseEngine(
        PulseConfig(tick_seconds=1.0, size_usd=5.0, min_seconds_since_open=0.0,
                    sigma_trust_floor=0.0, min_vol_samples=2, directional_down_only=True,
                    prism_enabled=True, prism_agent_gate_enabled=True,
                    data_dir=str(tmp_path), fresh_start=True),
        # both sides ~0.55 (asks sum 1.10, bids sum 1.00 -> no dutch-book arb) but far enough from
        # fair that the down-only edge E<=0 -> rank 0 -> no agent.
        market_feed=_Mkt(win, up_ask=0.55, down_ask=0.55),
        price_feed=_feed())
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    eng.tick(now=t0 + 40)

    rbs = eng.reconciler.report()["rejected_by_stage"]
    assert rbs.get("prism_agent", 0) >= 1
    assert eng.ledger.trades == 0


def test_arb_lane_unaffected_by_prism(tmp_path):
    """Regression: enabling PRISM must not disturb the risk-free arb scan/report."""
    t0 = 6_200_000.0
    win = _mk_window(t0)
    eng = PulseEngine(
        PulseConfig(tick_seconds=1.0, size_usd=5.0, min_seconds_since_open=0.0,
                    sigma_trust_floor=0.0, min_vol_samples=2,
                    prism_enabled=True, data_dir=str(tmp_path), fresh_start=True),
        market_feed=_Mkt(win), price_feed=_feed())
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    eng.tick(now=t0 + 40)
    arb = eng.status().get("arbitrage") or {}
    assert "arb_scan_count" in arb                       # arb report still present/intact
    assert "reconciliation" in eng.status()              # accounting bundle intact


def test_prism_sizing_positive_for_high_rank_bucket(tmp_path):
    """A proven Thompson bucket + strong ensemble -> Sniper size from the allocator > base $5."""
    from engine.pulse.prism.agents import AgentKind, CapitalAllocator, AgentConfig
    from engine.pulse.prism.thompson import BucketKey

    alloc = CapitalAllocator(bankroll_usd=500.0 * 0.10, cfg=AgentConfig())  # $50 bankroll
    # high R, high C, proven bucket (thompson_mult high), cheap-ish ask -> sniper size
    s = alloc.size_usd(AgentKind.SNIPER, R=0.30, C=0.90, ask=0.45, depth_usd=800,
                       thompson_mult=0.9, p_win=0.75)
    assert s.agent == AgentKind.SNIPER
    assert s.size_usd > 5.0                              # bigger than the flat base size
    assert s.size_usd <= 200.0
