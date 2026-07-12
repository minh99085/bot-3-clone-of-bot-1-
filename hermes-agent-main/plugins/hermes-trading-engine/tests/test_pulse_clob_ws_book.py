"""CLOB WS live-book maintenance + the WS arb trigger.

The fast scanner reads a live book maintained from the WS stream (book snapshots + price_change
deltas) and only REST-confirms when the WS complete set is near a crossing. Pins: a book snapshot
builds an OrderBook; price_change deltas update/remove levels; stale/missing books return None; and
the trigger fires only near a $1 crossing (so the efficient ~1.01/0.99 state skips the REST call).
"""

from __future__ import annotations

import time

from engine.pulse.clob_feed import ClobBookFeed


def _book_ev(aid, bids, asks):
    return {"event_type": "book", "asset_id": aid, "tick_size": "0.01",
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


def test_book_snapshot_builds_order_book():
    cf = ClobBookFeed(websocket_enabled=True)
    cf._ingest(_book_ev("A", bids=[(0.49, 100), (0.48, 200)], asks=[(0.51, 100), (0.52, 150)]))
    ob = cf.order_book("A")
    assert ob is not None
    assert ob.best_bid == 0.49 and ob.best_ask == 0.51        # best bid = highest, best ask = lowest
    assert cf.order_book("MISSING") is None


def test_price_change_deltas_update_and_remove_levels():
    cf = ClobBookFeed(websocket_enabled=True)
    cf._ingest(_book_ev("A", bids=[(0.49, 100)], asks=[(0.51, 100)]))
    # a better ask appears at 0.50, and the 0.51 level is removed (size 0)
    cf._ingest({"event_type": "price_change", "asset_id": "A",
                "changes": [{"price": "0.50", "size": "80", "side": "SELL"},
                            {"price": "0.51", "size": "0", "side": "SELL"}]})
    ob = cf.order_book("A")
    assert ob.best_ask == 0.50                                # delta applied
    assert all(p != 0.51 for p, _ in ob.asks)                 # removed level gone


def test_stale_book_returns_none():
    cf = ClobBookFeed(websocket_enabled=True)
    cf._ingest(_book_ev("A", bids=[(0.49, 100)], asks=[(0.51, 100)]))
    cf._books["A"]["ts"] = time.time() - 999                  # force stale
    assert cf.order_book("A", max_age_s=30.0) is None


def test_ws_trigger_only_near_crossing():
    from engine.pulse.engine import PulseEngine  # noqa: F401 — ensure import path
    # build a tiny stand-in with the method via the unbound function on a dummy holding clob_feed
    cf = ClobBookFeed(websocket_enabled=True)

    class _W:
        up_token_id = "U"; down_token_id = "D"

    class _Eng:
        _ws_arb_trigger = PulseEngine._ws_arb_trigger

    eng = _Eng()
    # efficient state: up ask 0.51 + down ask 0.51 = 1.02 (>1.003); bids 0.49+0.49=0.98 (<0.997) -> SKIP
    cf._ingest(_book_ev("U", bids=[(0.49, 100)], asks=[(0.51, 100)]))
    cf._ingest(_book_ev("D", bids=[(0.49, 100)], asks=[(0.51, 100)]))
    assert eng._ws_arb_trigger(cf, _W()) is False
    # buy-both crossing: up ask 0.45 + down ask 0.45 = 0.90 < 1 -> TRIGGER (REST confirm)
    cf._ingest(_book_ev("U", bids=[(0.44, 100)], asks=[(0.45, 100)]))
    cf._ingest(_book_ev("D", bids=[(0.44, 100)], asks=[(0.45, 100)]))
    assert eng._ws_arb_trigger(cf, _W()) is True
    # no WS book -> trigger (fall back to REST)
    assert eng._ws_arb_trigger(cf, type("W2", (), {"up_token_id": "X", "down_token_id": "Y"})()) is True
