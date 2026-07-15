"""Dashboard data accessors — read STATE, LESSONS, ledgers for Streamlit UI."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes.state_io import (
    DATA,
    knowledge_path,
    parse_state_fields,
    read_jsonl,
    read_lessons_md,
    read_state_md,
    read_text,
)

STARTING_BANKROLL = 2000.0


def paper_dir() -> Path:
    p = DATA / "paper"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_state() -> dict[str, Any]:
    fields = parse_state_fields(read_state_md())
    capital = float(
        fields.get("capital_usd")
        or fields.get("capital")
        or fields.get("starting_bankroll_usd")
        or STARTING_BANKROLL
    )
    fields["capital_usd"] = capital
    fields["starting_bankroll_usd"] = float(
        fields.get("starting_bankroll_usd") or STARTING_BANKROLL
    )
    return fields


def load_trades() -> list[dict[str, Any]]:
    rows = read_jsonl(paper_dir() / "trade_ledger.jsonl")
    return rows


def load_pretrade() -> list[dict[str, Any]]:
    return read_jsonl(paper_dir() / "pretrade_decisions.jsonl")


def load_fills() -> list[dict[str, Any]]:
    return [r for r in load_trades() if r.get("event") == "fill"]


def load_positions_open() -> list[dict[str, Any]]:
    opens = [r for r in load_trades() if r.get("event") == "position_open"]
    settles = {
        r.get("signal_id") or r.get("position_id")
        for r in load_trades()
        if r.get("event") == "settlement"
    }
    # Keep opens whose signal not yet settled (best-effort)
    out = []
    for o in opens:
        sid = o.get("signal_id")
        if sid and sid in settles:
            continue
        out.append(o)
    return out[-50:]


def load_settlements() -> list[dict[str, Any]]:
    return [
        r
        for r in load_trades()
        if r.get("event") == "settlement" or r.get("won") is not None
    ]


def equity_curve(starting: float = STARTING_BANKROLL) -> list[dict[str, Any]]:
    """Cumulative equity from settlements."""
    eq = starting
    curve = [{"t": "start", "equity": starting, "pnl": 0.0}]
    for s in load_settlements():
        pnl = float(s.get("pnl_usd", 0) or 0)
        eq += pnl
        curve.append(
            {
                "t": s.get("settled_at") or s.get("filled_at") or "",
                "equity": round(eq, 2),
                "pnl": round(pnl, 2),
                "won": bool(s.get("won") or pnl > 0),
                "market_id": s.get("market_id"),
                "substrategy_id": s.get("substrategy_id", ""),
            }
        )
    return curve


def total_pnl(starting: float = STARTING_BANKROLL) -> float:
    curve = equity_curve(starting)
    return round(curve[-1]["equity"] - starting, 2) if curve else 0.0


def substrategy_cards() -> list[dict[str, Any]]:
    settles = load_settlements()
    buckets: dict[str, list[dict]] = {}
    for s in settles:
        sid = s.get("substrategy_id") or (
            f"{s.get('entry_mode')}|{s.get('regime')}|h{s.get('hourly_bucket')}"
        )
        buckets.setdefault(str(sid), []).append(s)

    # Weights from latest portfolio snapshot if present
    snaps = read_jsonl(paper_dir() / "portfolio_snapshots.jsonl")
    top_w = snaps[-1].get("top_weights", {}) if snaps else {}

    cards = []
    for sid, rows in buckets.items():
        wins = sum(1 for r in rows if r.get("won") or float(r.get("pnl_usd", 0)) > 0)
        pnls = [float(r.get("pnl_usd", 0)) for r in rows]
        sizes = [float(r.get("size_usd", 1) or 1) for r in rows]
        rets = [p / sz for p, sz in zip(pnls, sizes)]
        recent = rows[-5:]
        recent_wr = sum(
            1 for r in recent if r.get("won") or float(r.get("pnl_usd", 0)) > 0
        ) / max(1, len(recent))
        cards.append(
            {
                "substrategy_id": sid,
                "n": len(rows),
                "wr": wins / len(rows),
                "ev": sum(rets) / len(rets) if rets else 0.0,
                "pnl": sum(pnls),
                "weight": float(top_w.get(sid, 0.0)),
                "recent_wr": recent_wr,
                "trend": "up" if recent_wr >= (wins / len(rows)) else "down",
            }
        )
    cards.sort(key=lambda c: -c["n"])
    return cards


def recent_lessons(limit: int = 8) -> list[str]:
    text = read_lessons_md()
    rules = []
    for m in __import__("re").finditer(r"\*\*Rule\*\*:\s*(.+)", text):
        rules.append(m.group(1).strip())
    return rules[-limit:][::-1]


def bandit_dashboard_state() -> dict[str, Any]:
    try:
        from hermes.bandit import get_bandit

        return get_bandit().summary()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def mispricing_dashboard_snapshot() -> dict[str, Any]:
    """Latest mispricing + CEX mid for the desk."""
    out: dict[str, Any] = {"cex_mid": None, "source": "none"}
    try:
        from connectors.cex_realtime import get_btc_snapshot

        snap = get_btc_snapshot(force_rest=True)
        out["cex_mid"] = snap.mid
        out["momentum"] = snap.momentum
        out["ret_60s"] = snap.ret_60s
        out["ret_3m"] = snap.ret_3m
        out["sources_agree"] = snap.sources_agree
        out["source"] = (snap.binance.source if snap.binance else "none")
        out["bybit"] = snap.bybit.price if snap.bybit else None
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    pts = load_pretrade()[-20:]
    if pts:
        mp = [p for p in pts if p.get("mispricing_active")]
        out["recent_mispricing_n"] = len(mp)
        out["recent_bandit_arms"] = [p.get("bandit_arm") for p in pts[-8:]]
        if mp:
            out["last_dislocation"] = mp[-1].get("mispricing_dislocation")
            out["last_entry_source"] = mp[-1].get("entry_source")
        enh = [p for p in pts if p.get("enhanced_passes")]
        out["enhanced_pass_n"] = len(enh)
        out["last_kelly_f"] = (enh[-1].get("kelly_f") if enh else None)
        out["last_enhanced_conviction"] = (
            enh[-1].get("enhanced_conviction") if enh else None
        )
        out["last_risk_unit"] = enh[-1].get("risk_unit") if enh else None
    return out


def scoped_market_cards() -> list[dict[str, Any]]:
    """Performance cards for the two allowed BTC up/down series only."""
    from hermes.market_scope import SERIES_5M, SERIES_15M, preferred_slugs

    settles = load_settlements()
    pretrades = load_pretrade()
    cards = []
    for series, label in (
        (SERIES_15M, "BTC Up/Down 15m"),
        (SERIES_5M, "BTC Up/Down 5m"),
    ):
        rows = [
            s
            for s in settles
            if str(s.get("market_series", "")).startswith(series)
            or series in str(s.get("substrategy_id", ""))
            or (series.endswith("15m") and "15m" in str(s.get("slug", "")))
            or (series.endswith("5m") and "5m" in str(s.get("slug", "")) and "15m" not in str(s.get("slug", "")))
        ]
        pts = [
            p
            for p in pretrades
            if series in str(p.get("substrategy_id", ""))
        ]
        wins = sum(1 for r in rows if r.get("won") or float(r.get("pnl_usd", 0)) > 0)
        pnls = [float(r.get("pnl_usd", 0)) for r in rows]
        last_pt = pts[-1] if pts else {}
        pref = [s for s in preferred_slugs() if ("15m" in s) == series.endswith("15m")]
        cards.append(
            {
                "series": series,
                "label": label,
                "preferred_slug": pref[0] if pref else "",
                "n": len(rows),
                "wr": (wins / len(rows)) if rows else None,
                "pnl": sum(pnls) if pnls else 0.0,
                "current_size_pct": last_pt.get("recommended_size_pct"),
                "current_size_usd": last_pt.get("recommended_size_usd"),
                "last_skip": last_pt.get("skip"),
                "last_reasons": last_pt.get("reasons") or [],
                "last_live_ev": last_pt.get("live_ev"),
            }
        )
    return cards


def recent_lessons_scoped(limit: int = 8) -> list[str]:
    """Lessons that mention the BTC up/down series."""
    text = read_lessons_md()
    rules = []
    for m in __import__("re").finditer(
        r"### \[.*?\][\s\S]*?\*\*Rule\*\*:\s*(.+)", text
    ):
        rule = m.group(1).strip()
        block = m.group(0).lower()
        if any(
            k in block or k in rule.lower()
            for k in (
                "btc_updown",
                "btc-updown",
                "5m",
                "15m",
                "aggressive:",
                "conservative:",
                "size_up",
                "size_down",
            )
        ):
            rules.append(rule)
    return rules[-limit:][::-1]


def oracle_alignment_snapshot() -> dict[str, Any]:
    """Best-effort live Chainlink vs implied Polymarket context."""
    out: dict[str, Any] = {
        "btc": None,
        "eth": None,
        "source": "none",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        from connectors.chainlink import ChainlinkClient

        cl = ChainlinkClient()
        btc = cl.get_price("BTC")
        eth = cl.get_price("ETH")
        out["btc"] = btc.price_usd
        out["eth"] = eth.price_usd
        out["source"] = btc.source
        out["btc_stale"] = btc.stale
        out["eth_stale"] = eth.stale
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    # Latest pretrade oracle alignments
    pts = load_pretrade()[-20:]
    if pts:
        aligns = [float(p.get("oracle_alignment", 0.5) or 0.5) for p in pts]
        out["avg_alignment"] = sum(aligns) / len(aligns)
    return out


def portfolio_metrics() -> dict[str, Any]:
    state = load_state()
    snaps = read_jsonl(paper_dir() / "portfolio_snapshots.jsonl")
    latest = snaps[-1] if snaps else {}
    return {
        "diversification_ratio": float(
            state.get("diversification_ratio")
            or latest.get("diversification_ratio")
            or 1.0
        ),
        "concentration_hhi": float(
            state.get("concentration_hhi") or latest.get("concentration_hhi") or 0.0
        ),
        "substrategies_active": int(
            state.get("substrategies_active")
            or latest.get("n_substrategies_active")
            or 0
        ),
        "cut": int(state.get("substrategies_cut") or latest.get("n_cut") or 0),
        "reduce": int(state.get("substrategies_reduce") or latest.get("n_reduce") or 0),
        "method": state.get("allocation_method") or latest.get("method") or "none",
        "top_weights": latest.get("top_weights") or {},
    }


def recent_trade_table(limit: int = 30) -> list[dict[str, Any]]:
    """Flatten fills + matching settlements for dashboard table."""
    fills = {f.get("signal_id"): f for f in load_fills()}
    rows = []
    for s in load_settlements()[-limit:]:
        f = fills.get(s.get("signal_id"), {})
        rows.append(
            {
                "market": s.get("market_id"),
                "direction": s.get("direction"),
                "entry": f.get("fill_price") or s.get("entry_price"),
                "exit": s.get("exit_price"),
                "won": s.get("won"),
                "pnl": s.get("pnl_usd"),
                "size": s.get("size_usd") or f.get("size_usd"),
                "sleeve": s.get("substrategy_id", ""),
                "reason": s.get("notes") or "",
            }
        )
    # Also show unmatched recent fills as open
    settled_ids = {s.get("signal_id") for s in load_settlements()}
    for f in load_fills()[-limit:]:
        if f.get("signal_id") in settled_ids:
            continue
        rows.append(
            {
                "market": f.get("market_id"),
                "direction": f.get("direction"),
                "entry": f.get("fill_price"),
                "exit": "—",
                "won": "open",
                "pnl": 0,
                "size": f.get("size_usd"),
                "sleeve": "",
                "reason": "open paper position",
            }
        )
    return rows[-limit:][::-1]
