"""Open snapshot persistence + scaled 15m lag tolerance."""

from engine.pulse.price import PulsePriceFeed, OpenSnapshot


def test_effective_max_open_lag_scales_for_15m():
    feed = PulsePriceFeed(fetcher=lambda: 60000.0, max_open_lag_s=120.0,
                          max_open_lag_15m_s=240.0)
    assert feed.effective_max_open_lag(900) == 240.0
    assert feed.effective_max_open_lag(300) == 120.0


def test_open_snapshot_persist_roundtrip():
    feed = PulsePriceFeed(fetcher=lambda: 61000.0, source_name="test",
                          max_open_lag_s=120.0, max_open_lag_15m_s=240.0)
    feed.poll(now=1000.0)
    feed.snapshot_open("win-1", open_ts=1000.0, now=1050.0)
    state = feed.to_open_state()
    assert len(state) == 1
    assert state[0]["key"] == "win-1"

    feed2 = PulsePriceFeed(fetcher=lambda: 61000.0, source_name="test")
    assert feed2.load_open_state(state) == 1
    snap = feed2.open_snapshot("win-1")
    assert snap is not None
    assert snap.price == 61000.0
    # snap_ts is the source observation nearest the boundary, not when the window was discovered.
    assert snap.lag_s == 0.0


def test_open_snapshot_does_not_substitute_late_current_price():
    feed = PulsePriceFeed(fetcher=lambda: 61000.0, source_name="test",
                          max_open_lag_s=3.0, max_open_lag_15m_s=3.0)
    feed.poll(now=1010.0)
    assert feed.snapshot_open("late", open_ts=1000.0, now=1010.0,
                              window_seconds=900) is None


def test_15m_lag_180s_not_late_with_240_cap():
    feed = PulsePriceFeed(fetcher=lambda: 60000.0, max_open_lag_15m_s=240.0)
    feed.poll(now=0.0)
    snap = feed.snapshot_open("w15", open_ts=0.0, now=180.0)
    assert snap is not None
    assert snap.lag_s <= feed.effective_max_open_lag(900)
