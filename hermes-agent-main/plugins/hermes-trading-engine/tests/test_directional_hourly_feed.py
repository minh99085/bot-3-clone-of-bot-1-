"""Tests for directional 1h Polymarket feed (up-down + above strike quartet)."""

from __future__ import annotations

import json

from engine.pulse.directional_hourly_feed import (
    DirectionalHourlyMarketFeed,
    parse_hourly_event,
    parse_hourly_market_record,
    _parse_strike,
)


def _http_factory(fixtures: dict):
    def _http(url: str, params: dict):
        key = (url, json.dumps(params, sort_keys=True))
        if key in fixtures:
            return 200, fixtures[key]
        slug = params.get("slug")
        if slug and slug in fixtures:
            return 200, fixtures[slug]
        return 404, None
    return _http


def test_parse_strike_from_slug():
    assert _parse_strike("bitcoin-above-62800-on-july-7-2026-1am-et") == 62800.0
    assert _parse_strike("ethereum-above-1740-on-july-7-2026-1am-et") == 1740.0
    assert _parse_strike("x", "Ethereum above 1,740 on July 7?") == 1740.0


def test_parse_updown_event():
    ev = {
        "id": "668327",
        "slug": "bitcoin-up-or-down-july-7-2026-12am-et",
        "title": "Bitcoin Up or Down - July 7, 12AM ET",
        "endDate": "2026-07-07T05:00:00Z",
        "markets": [{
            "id": "2801732",
            "slug": "bitcoin-up-or-down-july-7-2026-12am-et",
            "question": "Bitcoin Up or Down - July 7, 12AM ET",
            "endDate": "2026-07-07T05:00:00Z",
            "startDate": "2026-07-05T04:00:12Z",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["111", "222"]',
        }],
    }
    w = parse_hourly_event(ev, series_slug="btc-up-or-down-hourly", series_label="btc_1h")
    assert w is not None
    assert w.market_kind == "updown"
    assert w.up_token_id == "111"
    assert w.down_token_id == "222"
    assert w.close_ts == w.open_ts + 3600
    assert w.directional_lane is True


def test_parse_above_market_yes_no():
    m = {
        "id": "2825335",
        "slug": "bitcoin-above-62800-on-july-7-2026-1am-et",
        "question": "Bitcoin above 62,800 on July 7, 1AM ET?",
        "endDate": "2026-07-07T05:00:00Z",
        "startDate": "2026-07-07T03:42:26Z",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["333", "444"]',
    }
    w = parse_hourly_market_record(m, series_label="btc_above")
    assert w is not None
    assert w.market_kind == "above"
    assert w.strike_price == 62800.0
    assert w.up_token_id == "333"
    assert w.down_token_id == "444"


def test_explicit_slug_fetch():
    ev_fixture = [{
        "id": "e1",
        "slug": "ethereum-up-or-down-july-7-2026-12am-et",
        "title": "ETH up/down",
        "endDate": "2026-07-07T05:00:00Z",
        "markets": [{
            "id": "m1",
            "slug": "ethereum-up-or-down-july-7-2026-12am-et",
            "question": "ETH up/down",
            "endDate": "2026-07-07T05:00:00Z",
            "startDate": "2026-07-07T04:00:00Z",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["u", "d"]',
        }],
    }]

    def _http(url: str, params: dict):
        if params.get("slug") == "ethereum-up-or-down-july-7-2026-12am-et":
            return 200, ev_fixture
        return 404, None

    feed = DirectionalHourlyMarketFeed(
        explicit_slugs=("ethereum-up-or-down-july-7-2026-12am-et",),
        auto_discover=False,
        http_get=_http,
    )
    w = feed.fetch_by_slug("ethereum-up-or-down-july-7-2026-12am-et")
    assert w is not None
    now = w.open_ts + 120.0
    wins = feed.active_windows(now=now)
    assert len(wins) == 1
    assert wins[0].slug == "ethereum-up-or-down-july-7-2026-12am-et"
