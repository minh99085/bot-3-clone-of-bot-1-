"""Verifiable stop conditions — agent-independent kill switches."""

from __future__ import annotations

from engine.pulse.stop_conditions import StopConfig, evaluate_directional, StrategyStopMonitor
from engine.pulse.state import build_state_md


class _Pos:
    def __init__(self, *, entry_ts, entry_price, pnl_usd, won, status="settled"):
        self.entry_ts = entry_ts
        self.entry_price = entry_price
        self.pnl_usd = pnl_usd
        self.won = won
        self.status = status


def _losing_streak(n=35):
    return [_Pos(entry_ts=float(i), entry_price=0.55, pnl_usd=-5.0, won=False)
            for i in range(n)]


def test_directional_not_halted_with_insufficient_samples():
    cfg = StopConfig(min_samples=30)
    out = evaluate_directional(positions=_losing_streak(10), ledger_stats={"max_drawdown_usd": 5},
                               starting_capital=500, cfg=cfg)
    assert out["halted"] is False
    assert out["reasons"] == ["insufficient_samples"]


def test_directional_not_halted_when_hairline_wr_below_be_but_pf_profitable():
    """VPS case: WR 62.8% vs BE 63.0% with PF 1.03 must not halt."""
    cfg = StopConfig(min_samples=30, min_profit_factor=0.85, max_drawdown_pct=99)
    wins = [_Pos(entry_ts=float(i), entry_price=0.63, pnl_usd=3.0, won=True) for i in range(27)]
    losses = [_Pos(entry_ts=float(100 + i), entry_price=0.63, pnl_usd=-5.0, won=False)
              for i in range(16)]
    out = evaluate_directional(positions=wins + losses,
                               ledger_stats={"max_drawdown_usd": 20},
                               starting_capital=500, cfg=cfg)
    assert out["halted"] is False
    assert "wilson_wr_below_breakeven" not in out["reasons"]
    assert out["metrics"]["profit_factor"] is not None
    assert out["metrics"]["profit_factor"] >= 1.0


def test_directional_not_halted_when_wilson_low_but_wr_and_pf_ok():
    """Wide Wilson CI on high avg entry must not halt a demonstrably profitable window."""
    cfg = StopConfig(min_samples=30, min_profit_factor=0.85, max_drawdown_pct=99)
    wins = [_Pos(entry_ts=float(i), entry_price=0.63, pnl_usd=2.0, won=True) for i in range(26)]
    losses = [_Pos(entry_ts=float(100 + i), entry_price=0.63, pnl_usd=-4.0, won=False)
              for i in range(15)]
    out = evaluate_directional(positions=wins + losses,
                               ledger_stats={"max_drawdown_usd": 20},
                               starting_capital=500, cfg=cfg)
    assert out["halted"] is False
    assert "wilson_wr_below_breakeven" not in out["reasons"]


def test_directional_halted_on_low_profit_factor():
    cfg = StopConfig(min_samples=30, min_profit_factor=0.85, max_drawdown_pct=99)
    out = evaluate_directional(positions=_losing_streak(35),
                               ledger_stats={"max_drawdown_usd": 50},
                               starting_capital=500, cfg=cfg)
    assert out["halted"] is True
    assert out["reasons"]


def test_directional_halted_on_drawdown_pct():
    cfg = StopConfig(min_samples=5, min_profit_factor=0.0, max_drawdown_pct=5.0)
    wins = [_Pos(entry_ts=float(i), entry_price=0.55, pnl_usd=3.0, won=True) for i in range(10)]
    out = evaluate_directional(positions=wins, ledger_stats={"max_drawdown_usd": 40},
                               starting_capital=500, cfg=cfg)
    assert out["halted"] is True
    assert "max_drawdown_pct_breach" in out["reasons"]


def test_stop_monitor_is_halted():
    mon = StrategyStopMonitor(cfg=StopConfig(min_samples=30, min_profit_factor=0.85))
    mon.refresh(directional_positions=_losing_streak(10),
                directional_stats={"max_drawdown_usd": 1},
                starting_capital=500)
    assert mon.is_halted("directional") is False
    mon.refresh(directional_positions=_losing_streak(35),
                directional_stats={"max_drawdown_usd": 1},
                starting_capital=500)
    assert mon.is_halted("directional") is True


def test_state_md_contains_sections():
    md = build_state_md(
        status={"ticks": 1, "ts": 1_700_000_000, "capital": {"starting_capital_usd": 500},
                "ledger": {"settled": 0}, "config": {}, "grok_decider": {"mode": "shadow"},
                "verifier": {"enabled": True}, "readiness": {"status": "not_ready"},
                "tradingview": {"context_gate": {"enabled": True}}},
        ledger={"positions": [], "stats": {}},
        stop_conditions={"strategies": {"directional": {"halted": False, "reasons": []}}},
        lessons={"recent": []})
    assert "# Hermes BTC Pulse — STATE" in md
    assert "## Capital" in md
    assert "## Verifiable stop conditions" in md
