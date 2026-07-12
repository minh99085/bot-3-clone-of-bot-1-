"""Rolling settled-trade decision history for Grok context (last N trades + aggregates).

PAPER ONLY. Observe-only memory — does not gate trades; feeds the Grok decision bundle so the
decider can learn from recent bot outcomes, its own prior calls, and verifier outcomes.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Optional


def _grok_alignment(grok: Optional[dict], side: str, outcome_up: bool) -> dict:
    g = grok or {}
    act = g.get("action")
    p_up = g.get("p_up")
    view_side = None
    if p_up is not None:
        try:
            view_side = "up" if float(p_up) >= 0.5 else "down"
        except (TypeError, ValueError):
            view_side = None
    action_match = (act == side) if act in ("up", "down") else None
    view_match = (view_side == side) if view_side else None
    outcome_side = "up" if outcome_up else "down"
    action_correct = (act == outcome_side) if act in ("up", "down") else None
    view_correct = (view_side == outcome_side) if view_side else None
    return {
        "action": act,
        "p_up": (round(float(p_up), 4) if p_up is not None else None),
        "action_matched_side": action_match,
        "view_matched_side": view_match,
        "action_correct": action_correct,
        "view_correct": view_correct,
    }


def _profit_factor(wins_pnl: float, losses_pnl: float) -> Optional[float]:
    if losses_pnl <= 0:
        return None if wins_pnl <= 0 else 999.0
    return round(wins_pnl / losses_pnl, 4)


class TradeDecisionHistory:
    """Ring buffer of the last ``max_trades`` settled paper trades with rolling aggregates."""

    def __init__(self, max_trades: int = 50):
        self.max_trades = int(max_trades)
        self._rows: deque = deque(maxlen=self.max_trades)

    def record_settled(self, *, decision_id: str, title: str, side: str, entry_mode: str,
                       entry_price: float, size_usd: float, outcome_up: bool, won: bool,
                       pnl_usd: float, research: Optional[dict] = None,
                       grok: Optional[dict] = None, verifier: Optional[dict] = None) -> None:
        rt = research or {}
        outcome = "up" if outcome_up else "down"
        row = {
            "decision_id": decision_id,
            "title": (str(title or "")[:48] or None),
            "side": side,
            "entry_mode": entry_mode or "unknown",
            "entry_price": round(float(entry_price), 4),
            "size_usd": round(float(size_usd), 2),
            "outcome": outcome,
            "won": bool(won),
            "pnl_usd": round(float(pnl_usd or 0.0), 4),
            "grok": _grok_alignment(grok, side, outcome_up),
            "verifier": ({
                "approved": verifier.get("approved"),
                "reason": str(verifier.get("reason") or "")[:120] or None,
            } if verifier else None),
            "edge_score": rt.get("edge_score_bucket"),
            "ttc_bucket": rt.get("edge_ttc_bucket") or rt.get("ttc_bucket"),
            "cex_agreement": rt.get("edge_cex_agreement"),
            "stale_divergence": rt.get("edge_stale_divergence"),
            "hurst_regime": rt.get("hurst_regime"),
        }
        self._rows.append(row)

    def recent(self, n: Optional[int] = None) -> list:
        rows = list(self._rows)
        if n is not None:
            rows = rows[-int(n):]
        return rows

    def aggregates(self) -> dict:
        rows = list(self._rows)
        n = len(rows)
        if not n:
            return {"n": 0, "note": "no settled trades in history buffer yet"}
        wins = sum(1 for r in rows if r.get("won"))
        pnl = round(sum(float(r.get("pnl_usd") or 0.0) for r in rows), 4)
        gw = sum(float(r["pnl_usd"]) for r in rows if (r.get("pnl_usd") or 0) > 0)
        gl = sum(-float(r["pnl_usd"]) for r in rows if (r.get("pnl_usd") or 0) < 0)
        up_rows = [r for r in rows if r.get("side") == "up"]
        dn_rows = [r for r in rows if r.get("side") == "down"]
        modes: dict = {}
        for r in rows:
            m = r.get("entry_mode") or "unknown"
            modes.setdefault(m, {"n": 0, "wins": 0, "pnl_usd": 0.0})
            modes[m]["n"] += 1
            modes[m]["wins"] += int(bool(r.get("won")))
            modes[m]["pnl_usd"] = round(modes[m]["pnl_usd"] + float(r.get("pnl_usd") or 0.0), 4)
        for m in modes:
            mn = modes[m]["n"]
            modes[m]["win_rate"] = round(modes[m]["wins"] / mn, 4) if mn else None

        def _wr(subset):
            if not subset:
                return None
            return round(sum(1 for r in subset if r.get("won")) / len(subset), 4)

        grok_action = [r for r in rows if (r.get("grok") or {}).get("action") in ("up", "down")]
        grok_view = [r for r in rows if (r.get("grok") or {}).get("p_up") is not None]
        grok_action_correct = [r for r in grok_action
                               if (r.get("grok") or {}).get("action_correct") is True]
        grok_view_correct = [r for r in grok_view
                             if (r.get("grok") or {}).get("view_correct") is True]

        return {
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4),
            "pnl_usd": pnl,
            "profit_factor": _profit_factor(gw, gl),
            "avg_pnl_usd": round(pnl / n, 4),
            "win_rate_up": _wr(up_rows),
            "win_rate_down": _wr(dn_rows),
            "up_n": len(up_rows),
            "down_n": len(dn_rows),
            "by_entry_mode": modes,
            "grok_action_accuracy": (round(len(grok_action_correct) / len(grok_action), 4)
                                     if grok_action else None),
            "grok_view_accuracy": (round(len(grok_view_correct) / len(grok_view), 4)
                                   if grok_view else None),
            "grok_graded_n": len(grok_view),
        }

    def view_for_grok(self, n: Optional[int] = None) -> dict:
        """Compact payload injected into the Grok decision bundle."""
        lim = int(n) if n is not None else self.max_trades
        return {
            "schema": "trade_decision_history/1.0",
            "max_trades": self.max_trades,
            "trades": self.recent(lim),
            "aggregates": self.aggregates(),
            "note": ("last settled paper trades newest-last; use aggregates + per-trade grok "
                     "alignment to avoid repeating losing patterns"),
        }

    def to_state(self) -> dict:
        return {"max_trades": self.max_trades, "rows": list(self._rows)}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.max_trades = int(data.get("max_trades") or self.max_trades)
        self._rows = deque(maxlen=self.max_trades)
        for row in (data.get("rows") or []):
            if isinstance(row, dict):
                self._rows.append(row)

    def backfill_from_positions(self, positions: list) -> int:
        """Rebuild buffer from settled ledger positions (sorted oldest→newest). Returns count added."""
        if self._rows:
            return 0
        settled = []
        for p in positions:
            if hasattr(p, "status"):
                if p.status != "settled":
                    continue
                pos = p
            elif isinstance(p, dict) and p.get("status") == "settled":
                pos = p
            else:
                continue
            settled.append(pos)
        if not settled:
            return 0
        settled.sort(key=lambda x: (getattr(x, "close_ts", None) or getattr(x, "entry_ts", None)
                                    or (x.get("close_ts") if isinstance(x, dict) else 0)
                                    or (x.get("entry_ts") if isinstance(x, dict) else 0)))
        added = 0
        for pos in settled[-self.max_trades:]:
            if hasattr(pos, "side"):
                rt = pos.research or {}
                self.record_settled(
                    decision_id=pos.decision_id or pos.window_key,
                    title=pos.title,
                    side=pos.side,
                    entry_mode=rt.get("entry_mode") or "unknown",
                    entry_price=float(pos.entry_price),
                    size_usd=float(pos.size_usd),
                    outcome_up=bool(pos.outcome_up),
                    won=bool(pos.won),
                    pnl_usd=float(pos.pnl_usd or 0.0),
                    research=rt,
                    grok=rt.get("grok_snapshot"),
                    verifier=rt.get("verifier_snapshot") or rt.get("verifier"),
                )
            else:
                rt = pos.get("research") or {}
                self.record_settled(
                    decision_id=pos.get("decision_id") or pos.get("window_key"),
                    title=pos.get("title"),
                    side=pos.get("side"),
                    entry_mode=rt.get("entry_mode") or "unknown",
                    entry_price=float(pos.get("entry_price") or 0),
                    size_usd=float(pos.get("size_usd") or 0),
                    outcome_up=bool(pos.get("outcome_up")),
                    won=bool(pos.get("won")),
                    pnl_usd=float(pos.get("pnl_usd") or 0.0),
                    research=rt,
                    grok=rt.get("grok_snapshot"),
                    verifier=rt.get("verifier_snapshot") or rt.get("verifier"),
                )
            added += 1
        return added