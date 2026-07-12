"""Tests for evidence-based GateAutoTuner (PAPER ONLY)."""

from types import SimpleNamespace

from engine.pulse.gate_auto_tune import GateAutoTuneConfig, GateAutoTuner


class _FakeTierCfg:
    sweet_min = 0.45
    sweet_max = 0.85
    strike_edge_min = 0.03
    harvest_edge_min = 0.02
    min_seconds_since_open = 300.0


class _FakeEngine:
    def __init__(self):
        self.cfg = SimpleNamespace(
            min_edge=0.03,
            min_entry_price=0.45,
            exec_min_ev_after_slippage=0.0,
            hourly_min_seconds_since_open=300.0,
        )
        self.hourly_entry_gate = SimpleNamespace(min_seconds_since_open=300.0)
        self.tier_engine = SimpleNamespace(cfg=_FakeTierCfg())
        self.osmani_loop = None


def test_tuner_disabled_noop():
    t = GateAutoTuner(GateAutoTuneConfig(enabled=False, cooldown_settlements=1, min_samples=1))
    eng = _FakeEngine()
    t.record_settled(won=False, pnl_usd=-5.0, entry_price=0.50)
    assert t.maybe_adjust(eng) is None


def test_tuner_tightens_on_low_wr():
    t = GateAutoTuner(GateAutoTuneConfig(
        enabled=True, lookback_n=20, min_samples=8, cooldown_settlements=8,
        kill_wr=0.55, target_wr=0.70, starve_fills_per_hour=0.1, rich_fills_per_hour=99.0,
    ))
    eng = _FakeEngine()
    base = 1_700_000_000.0
    # 8 losses in ~1 hour → WR=0, fills_per_hour high enough to not starve
    for i in range(8):
        t.record_settled(won=False, pnl_usd=-5.0, entry_price=0.50,
                         entry_ts=base + i * 400, now=base + i * 400)
    adj = t.maybe_adjust(eng)
    assert adj is not None
    assert adj["action"] == "tighten"
    assert eng.cfg.min_edge > 0.03
    assert eng.cfg.min_entry_price > 0.45
    assert eng.hourly_entry_gate.min_seconds_since_open > 300.0


def test_tuner_loosens_on_starvation():
    t = GateAutoTuner(GateAutoTuneConfig(
        enabled=True, lookback_n=20, min_samples=8, cooldown_settlements=8,
        kill_wr=0.40, target_wr=0.65, starve_fills_per_hour=2.0, rich_fills_per_hour=10.0,
    ))
    eng = _FakeEngine()
    eng.cfg.min_edge = 0.08
    eng.cfg.min_entry_price = 0.58
    eng.cfg.hourly_min_seconds_since_open = 1200.0
    eng.hourly_entry_gate.min_seconds_since_open = 1200.0
    # High WR but very sparse fills over a long span → loosen
    base = 1_700_000_000.0
    for i in range(8):
        t.record_settled(won=True, pnl_usd=3.0, entry_price=0.60,
                         entry_ts=base + i * 7200, now=base + i * 7200)  # every 2h
    adj = t.maybe_adjust(eng)
    assert adj is not None
    assert adj["action"] == "loosen"
    assert eng.cfg.min_edge < 0.08
    assert eng.cfg.min_entry_price < 0.58


def test_tuner_respects_cooldown():
    t = GateAutoTuner(GateAutoTuneConfig(
        enabled=True, lookback_n=20, min_samples=4, cooldown_settlements=10,
        kill_wr=0.55,
    ))
    eng = _FakeEngine()
    for i in range(5):
        t.record_settled(won=False, pnl_usd=-1.0, entry_price=0.5, now=1_700_000_000 + i)
    assert t.maybe_adjust(eng) is None  # cooldown not met


def test_tuner_clamps_bounds():
    t = GateAutoTuner(GateAutoTuneConfig(
        enabled=True, lookback_n=20, min_samples=8, cooldown_settlements=8,
        kill_wr=0.90, step_edge=0.50,  # huge step → must clamp
    ))
    eng = _FakeEngine()
    eng.cfg.min_edge = 0.09
    base = 1_700_000_000.0
    for i in range(8):
        t.record_settled(won=False, pnl_usd=-1.0, entry_price=0.5,
                         entry_ts=base + i * 60, now=base + i * 60)
    adj = t.maybe_adjust(eng)
    assert adj is not None
    assert eng.cfg.min_edge <= t.cfg.bounds.min_edge_hi


def test_tuner_state_roundtrip():
    t = GateAutoTuner(GateAutoTuneConfig(enabled=True, cooldown_settlements=1))
    t.record_settled(won=True, pnl_usd=2.0, entry_price=0.55, asset="btc")
    st = t.to_state()
    t2 = GateAutoTuner(GateAutoTuneConfig(enabled=True))
    t2.load_state(st)
    assert len(t2._recent) == 1
    assert t2.report()["enabled"] is True
