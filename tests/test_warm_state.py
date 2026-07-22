"""B2 — warm-start: price history + σ EWMA survive a simulated restart."""

from __future__ import annotations

import json
import time

import pytest


@pytest.fixture(autouse=True)
def _warm_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WARM_DIR", str(tmp_path / "warm"))
    yield tmp_path / "warm"


def test_price_history_roundtrip_drops_stale_ticks():
    from hermes.warm_state import load_price_history, save_price_history

    now = time.time()
    save_price_history({
        "BTC": [(now - 700, 63000.0), (now - 30, 64000.0), (now - 5, 64100.0)],
        "ETH": [(now - 900, 3000.0)],  # all stale → dropped entirely
    })
    restored = load_price_history()
    assert [p for _, p in restored["BTC"]] == [64000.0, 64100.0]
    assert "ETH" not in restored


def test_cex_history_survives_simulated_restart(monkeypatch):
    import connectors.cex_realtime as rt

    # process 1: ticks arrive and get snapshotted
    rt._ASSET_HISTORY.clear()
    rt._WARM_LOADED = True          # already "loaded" in process 1
    rt._push_asset_history("ETH", 3100.0)
    rt._last_warm_save = 0.0        # force the throttled save on the last push
    rt._push_asset_history("ETH", 3105.0)

    # process 2 (restart): fresh in-memory state, warm load on first read
    rt._ASSET_HISTORY.clear()
    rt._WARM_LOADED = False
    times, prices = rt.get_asset_price_history("ETH")
    assert prices == [3100.0, 3105.0]
    assert all(t > 0 for t in times)


def test_btc_feed_seeds_history_on_restart(monkeypatch):
    import connectors.cex_realtime as rt

    rt._ASSET_HISTORY.clear()
    rt._WARM_LOADED = True
    rt._push_asset_history("BTC", 64000.0)
    rt._last_warm_save = 0.0
    rt._push_asset_history("BTC", 64050.0)

    rt._ASSET_HISTORY.clear()
    rt._WARM_LOADED = False
    feed = rt.RealtimeBtcFeed()      # __init__ must warm-seed; no thread started
    _, prices = feed.get_price_history()
    assert prices == [64000.0, 64050.0]


def test_sigma_ewma_survives_simulated_restart():
    import hermes.mispricing as mp

    # process 1: learn a ratio ≠ default
    mp._SIGMA_RATIO_EWMA.clear()
    mp._SIGMA_WARM_LOADED = True
    learned = mp.update_sigma_ratio("BTC", implied=0.8, realized=1.0)
    assert learned == pytest.approx(0.8)

    # process 2 (restart): empty memory, must restore from disk
    mp._SIGMA_RATIO_EWMA.clear()
    mp._SIGMA_WARM_LOADED = False
    assert mp.sigma_ratio("BTC") == pytest.approx(0.8)


def test_stale_sigma_snapshot_ignored(_warm_dir):
    import hermes.mispricing as mp
    from hermes.warm_state import SIGMA_MAX_AGE_SEC

    _warm_dir.mkdir(parents=True, exist_ok=True)
    (_warm_dir / "sigma_ewma.json").write_text(json.dumps({
        "saved_at": time.time() - SIGMA_MAX_AGE_SEC - 60,
        "ratios": {"BTC": 0.5},
    }))
    mp._SIGMA_RATIO_EWMA.clear()
    mp._SIGMA_WARM_LOADED = False
    assert mp.sigma_ratio("BTC") == 1.0  # stale snapshot → default, not 0.5


def test_corrupt_warm_files_never_raise(_warm_dir):
    from hermes.warm_state import load_price_history, load_sigma_ewma

    _warm_dir.mkdir(parents=True, exist_ok=True)
    (_warm_dir / "price_history.json").write_text("{not json")
    (_warm_dir / "sigma_ewma.json").write_text("[broken")
    assert load_price_history() == {}
    assert load_sigma_ewma() == {}
