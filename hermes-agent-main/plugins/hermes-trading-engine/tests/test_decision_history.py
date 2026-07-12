"""Trade decision history ring buffer for Grok context."""

from engine.pulse.decision_history import TradeDecisionHistory


def _record(h, i, *, side="down", won=True, pnl=2.0, action="down", p_up=0.4):
    h.record_settled(
        decision_id=f"d{i}",
        title=f"win-{i}",
        side=side,
        entry_mode="standard",
        entry_price=0.6,
        size_usd=5.0,
        outcome_up=(side == "up" and won) or (side == "down" and not won),
        won=won,
        pnl_usd=pnl,
        research={"edge_score_bucket": "high", "edge_ttc_bucket": "180_240s"},
        grok={"action": action, "p_up": p_up, "confidence": 0.7},
        verifier={"approved": True, "reason": "ok"},
    )


def test_ring_buffer_max_50():
    h = TradeDecisionHistory(max_trades=50)
    for i in range(60):
        _record(h, i)
    assert len(h.recent()) == 50
    assert h.recent()[-1]["decision_id"] == "d59"


def test_aggregates_and_grok_view():
    h = TradeDecisionHistory(max_trades=50)
    _record(h, 1, won=True, pnl=3.0, action="down", p_up=0.35)
    _record(h, 2, won=False, pnl=-5.0, action="up", p_up=0.7, side="up")
    agg = h.aggregates()
    assert agg["n"] == 2
    assert agg["wins"] == 1
    assert agg["win_rate"] == 0.5
    assert agg["pnl_usd"] == -2.0
    view = h.view_for_grok()
    assert view["schema"] == "trade_decision_history/1.0"
    assert len(view["trades"]) == 2
    assert view["trades"][0]["grok"]["action_correct"] is True
    assert "aggregates" in view


def test_persist_and_backfill():
    h = TradeDecisionHistory(max_trades=50)
    _record(h, 1)
    state = h.to_state()
    h2 = TradeDecisionHistory(max_trades=50)
    h2.load_state(state)
    assert len(h2.recent()) == 1
    h3 = TradeDecisionHistory(max_trades=50)
    class P:
        status = "settled"
        decision_id = "x1"
        window_key = "x1"
        title = "t"
        side = "down"
        entry_price = 0.55
        size_usd = 5.0
        outcome_up = False
        won = True
        pnl_usd = 2.0
        research = {"entry_mode": "standard"}
        close_ts = 1.0
        entry_ts = 0.0
    assert h3.backfill_from_positions([P()]) == 1
    assert h3.recent()[0]["decision_id"] == "x1"