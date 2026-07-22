"""Dashboard data accessors — read STATE, LESSONS, ledgers for Streamlit UI."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes.state_io import (
    DATA,
    knowledge_path,
    ledger_path,
    parse_state_fields,
    read_jsonl,
    read_lessons_md,
    read_state_md,
    read_text,
)

STARTING_BANKROLL = 2000.0
PER_INSTANCE_BANKROLL = STARTING_BANKROLL
FLEET_INSTANCE_COUNT = 10
FLEET_BANKROLL = STARTING_BANKROLL * FLEET_INSTANCE_COUNT  # $20,000

# Compose instance_id → strategy variant (must match docker-compose.yml)
COMPOSE_LANES: tuple[tuple[str, str], ...] = (
    ("lane01_baseline", "baseline"),
    ("lane02_autonomy", "baseline"),  # same q as baseline; full autonomy stack on
    ("lane03_favorite", "favorite_only"),
    ("lane04_longshot", "longshot_only"),
    ("lane05_late", "late_window"),
    ("lane06_garch", "garch_sigma"),
    ("lane07_marketsigma", "market_sigma_gap"),
    ("lane08_legacy", "legacy_ensemble"),
    ("lane09_random", "random_null"),
    ("lane10_depth", "depth_aware"),
)

INSTANCE_IDS = tuple(iid for iid, _ in COMPOSE_LANES)

_LANE_ACCENTS = (
    "#38bdf8",  # baseline
    "#22d3ee",  # chainlink
    "#4ade80",  # favorite
    "#fbbf24",  # longshot
    "#fb923c",  # late
    "#a78bfa",  # garch
    "#34d399",  # market sigma
    "#f87171",  # legacy (neg control)
    "#94a3b8",  # random null
    "#2dd4bf",  # depth
)


def _build_instance_metas() -> list[dict[str, Any]]:
    """Lane cards for the 10× BTC15 paired experiment."""
    from hermes.lane_variants import LANES

    metas: list[dict[str, Any]] = []
    for i, (iid, variant) in enumerate(COMPOSE_LANES):
        spec = LANES.get(variant)
        if iid == "lane02_autonomy":
            short = "autonomy"
            subtitle = "barrier q + full autonomy stack (pure-vs-autonomy A/B)"
            role = "experiment"
            display_variant = "autonomy"
        else:
            short = variant.replace("_", " ")
            subtitle = spec.description if spec else variant
            role = "null" if "random" in variant else (
                "neg_control" if "legacy" in variant else (
                    "control" if variant == "baseline" else "experiment"
                )
            )
            display_variant = variant
        metas.append(
            {
                "id": iid,
                "label": f"{i + 1:02d} {short.title()}",
                "subtitle": subtitle,
                "variant": display_variant,
                "role": role,
                "filter": "btc15",
                "accent": _LANE_ACCENTS[i % len(_LANE_ACCENTS)],
                "series": ["btc_updown_15m"],
            }
        )
    return metas


INSTANCE_METAS: list[dict[str, Any]] = _build_instance_metas()


def instance_meta(instance_id: str) -> dict[str, Any]:
    for m in INSTANCE_METAS:
        if m["id"] == instance_id:
            return m
    return {
        "id": instance_id,
        "label": instance_id,
        "subtitle": instance_id,
        "filter": instance_id,
        "accent": "#94a3b8",
        "series": [],
    }


def paper_dir() -> Path:
    """Dashboard aggregates; prefer all instance folders under data/paper/."""
    p = DATA / "paper"
    p.mkdir(parents=True, exist_ok=True)
    return p


def instance_paper_dirs() -> list[Path]:
    """All per-instance paper dirs (fleet layout). Skip legacy flat ledger at root."""
    root = paper_dir()
    dirs: list[Path] = []
    for iid in INSTANCE_IDS:
        child = root / iid
        if child.is_dir():
            dirs.append(child)
    if dirs:
        return dirs
    # Legacy flat ledger only when no fleet subdirs exist
    if (root / "trade_ledger.jsonl").exists():
        return [root]
    return [root]


def load_state() -> dict[str, Any]:
    fields = parse_state_fields(read_state_md())
    per = float(
        fields.get("per_instance_bankroll_usd")
        or fields.get("starting_bankroll_usd")
        or STARTING_BANKROLL
    )
    fields["per_instance_bankroll_usd"] = per
    fields["starting_bankroll_usd"] = per  # legacy alias
    fields["fleet_bankroll_usd"] = per * FLEET_INSTANCE_COUNT
    fields["instance_count"] = FLEET_INSTANCE_COUNT
    fields["capital_usd"] = fields["fleet_bankroll_usd"]
    return fields


def trades_for_instance(instance_id: str) -> list[dict[str, Any]]:
    return [t for t in load_trades() if t.get("instance_id") == instance_id]


def settlements_for_instance(instance_id: str) -> list[dict[str, Any]]:
    return [
        t
        for t in trades_for_instance(instance_id)
        if t.get("event") == "settlement" or t.get("won") is not None
    ]


def pretrade_for_instance(instance_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in instance_paper_dirs():
        if d.name == instance_id or (instance_id == "legacy" and d.name == "paper"):
            rows.extend(read_jsonl(d / "pretrade_decisions.jsonl"))
    return rows


def equity_curve_for_instance(
    instance_id: str, starting: float = STARTING_BANKROLL
) -> list[dict[str, Any]]:
    eq = starting
    curve = [{"t": "start", "equity": starting, "pnl": 0.0, "instance_id": instance_id}]
    for s in settlements_for_instance(instance_id):
        pnl = float(s.get("pnl_usd", 0) or 0)
        eq += pnl
        curve.append(
            {
                "t": s.get("settled_at") or s.get("filled_at") or "",
                "equity": round(eq, 2),
                "pnl": round(pnl, 2),
                "won": bool(s.get("won") or pnl > 0),
                "instance_id": instance_id,
                "slug": s.get("slug") or "",
            }
        )
    return curve


def fleet_equity_curve(starting: float = FLEET_BANKROLL) -> list[dict[str, Any]]:
    """Aggregate fleet equity from all instance settlements (chronological)."""
    events: list[dict[str, Any]] = []
    for meta in INSTANCE_METAS:
        iid = meta["id"]
        for s in settlements_for_instance(iid):
            events.append({**s, "instance_id": iid})
    events.sort(key=lambda e: str(e.get("settled_at") or e.get("filled_at") or ""))

    eq = starting
    curve = [{"t": "start", "ts": "start", "equity": starting, "pnl": 0.0}]
    for s in events:
        pnl = float(s.get("pnl_usd", 0) or 0)
        eq += pnl
        ts = s.get("settled_at") or s.get("filled_at") or ""
        curve.append(
            {
                "t": ts,
                "ts": ts,
                "equity": round(eq, 2),
                "pnl": round(pnl, 2),
                "won": bool(s.get("won") or pnl > 0),
                "instance_id": s.get("instance_id"),
                "slug": s.get("slug") or "",
            }
        )
    return curve


def fleet_total_pnl(starting: float = FLEET_BANKROLL) -> float:
    curve = fleet_equity_curve(starting)
    return round(curve[-1]["equity"] - starting, 2) if curve else 0.0


def fleet_win_rate() -> Optional[float]:
    settles = []
    for meta in INSTANCE_METAS:
        settles.extend(settlements_for_instance(meta["id"]))
    if not settles:
        return None
    wins = sum(1 for s in settles if s.get("won") or float(s.get("pnl_usd", 0)) > 0)
    return wins / len(settles)


def open_positions_for_instance(instance_id: str) -> list[dict[str, Any]]:
    settles = {
        s.get("signal_id") or s.get("position_id")
        for s in settlements_for_instance(instance_id)
    }
    out = []
    for f in trades_for_instance(instance_id):
        if f.get("event") != "fill":
            continue
        sid = f.get("signal_id")
        if sid and sid in settles:
            continue
        out.append(f)
    return out


def instance_summary(instance_id: str) -> dict[str, Any]:
    """Per-container desk card: $2k bankroll + isolated ledger stats."""
    meta = instance_meta(instance_id)
    bankroll = STARTING_BANKROLL
    settles = settlements_for_instance(instance_id)
    wins = sum(1 for s in settles if s.get("won") or float(s.get("pnl_usd", 0)) > 0)
    losses = len(settles) - wins
    pnls = [float(s.get("pnl_usd", 0) or 0) for s in settles]
    curve = equity_curve_for_instance(instance_id, bankroll)
    equity = curve[-1]["equity"] if curve else bankroll
    pts = pretrade_for_instance(instance_id)
    last_pt = pts[-1] if pts else {}
    open_n = len(open_positions_for_instance(instance_id))
    has_activity = bool(settles or open_n or pts)
    status = "active" if (open_n or last_pt.get("skip") is False) else (
        "watching" if pts else "idle"
    )

    return {
        **meta,
        "instance_id": instance_id,
        "bankroll": bankroll,
        "equity": round(equity, 2),
        "pnl": round(equity - bankroll, 2),
        "n_settled": len(settles),
        "trades": len(settles),
        "wins": wins,
        "losses": losses,
        "wr": (wins / len(settles)) if settles else None,
        "win_rate": (wins / len(settles)) if settles else 0.0,
        "open_n": open_n,
        "open_positions": open_n,
        "status": status if has_activity or instance_id else "idle",
        "avg_size": (sum(float(s.get("size_usd", 0) or 0) for s in settles) / len(settles))
        if settles
        else None,
        "last_skip": last_pt.get("skip"),
        "last_slug": last_pt.get("slug") or (settles[-1].get("slug") if settles else ""),
        "last_reasons": last_pt.get("reasons") or [],
        "last_live_ev": last_pt.get("live_ev"),
        "current_size_usd": last_pt.get("recommended_size_usd"),
    }


def instance_cards() -> list[dict[str, Any]]:
    """One summary card per BTC15 strategy lane (lane01 … lane10)."""
    return [instance_summary(m["id"]) for m in INSTANCE_METAS]


def fleet_summary() -> dict[str, Any]:
    cards = instance_cards()
    fleet_eq = sum(c["equity"] for c in cards)
    fleet_pnl = fleet_eq - FLEET_BANKROLL
    total_settled = sum(c["n_settled"] for c in cards)
    total_open = sum(c["open_n"] for c in cards)
    total_wins = sum(c["wins"] for c in cards)
    total_losses = sum(c["losses"] for c in cards)
    with_data = sum(1 for c in cards if c["n_settled"] or c["open_n"])
    return {
        "fleet_bankroll": FLEET_BANKROLL,
        "per_instance_bankroll": STARTING_BANKROLL,
        "instance_count": FLEET_INSTANCE_COUNT,
        "fleet_equity": round(fleet_eq, 2),
        "fleet_pnl": round(fleet_pnl, 2),
        "total_pnl": round(fleet_pnl, 2),
        "fleet_wr": fleet_win_rate(),
        "win_rate": fleet_win_rate() or 0.0,
        "total_settled": total_settled,
        "total_trades": total_settled,
        "total_open": total_open,
        "open_positions": total_open,
        "wins": total_wins,
        "losses": total_losses,
        "instances_with_data": with_data,
        "instances": cards,
    }


def lane_scoreboard() -> dict[str, Any]:
    """Paired scoreboard vs random_null for the 10-lane BTC15 experiment."""
    from backtest.lane_compare import build_board
    from backtest.paper_ledger import load_trades

    root = paper_dir()
    allowed = set(INSTANCE_IDS)
    trades_by_lane: dict[str, list] = {}
    for iid in INSTANCE_IDS:
        ledger = root / iid / "trade_ledger.jsonl"
        if ledger.is_file():
            trades_by_lane[iid] = load_trades([ledger])
        else:
            trades_by_lane[iid] = []

    board = build_board(trades_by_lane)
    meta_by_id = {m["id"]: m for m in INSTANCE_METAS}
    rows: list[dict[str, Any]] = []
    for s in sorted(board.lanes, key=lambda x: (-x.paired_pnl_diff, -x.pnl)):
        if s.lane not in allowed:
            continue
        meta = meta_by_id.get(s.lane, {})
        rows.append(
            {
                "lane": s.lane,
                "label": meta.get("label", s.lane),
                "variant": meta.get("variant", s.lane),
                "role": meta.get("role", "experiment"),
                "n": s.n,
                "wr": s.wr,
                "pnl": round(s.pnl, 2),
                "avg_entry": round(s.avg_entry, 3),
                "n_paired": s.n_paired,
                "delta_vs_null": round(s.paired_pnl_diff, 2),
            }
        )
    return {
        "null_lane": board.null_lane,
        "n_shared_windows": board.n_shared_windows,
        "notes": list(board.notes),
        "rows": rows,
    }


def instance_trade_history(instance_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Trades for one instance (newest first)."""
    fills = {
        f.get("signal_id"): f
        for f in trades_for_instance(instance_id)
        if f.get("event") == "fill"
    }
    settled_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    meta = instance_meta(instance_id)
    label = meta.get("label") or instance_id

    for s in settlements_for_instance(instance_id):
        sid = s.get("signal_id")
        if sid:
            settled_ids.add(sid)
        f = fills.get(sid, {})
        rows.append(
            {
                "time": s.get("settled_at") or s.get("filled_at") or "",
                "lane": label,
                "instance_id": instance_id,
                "slug": s.get("slug") or f.get("slug") or "",
                "direction": s.get("direction") or f.get("direction"),
                "size": s.get("size_usd") or f.get("size_usd"),
                "entry": f.get("fill_price") or s.get("entry_price"),
                "exit": s.get("exit_price"),
                "won": s.get("won"),
                "pnl": s.get("pnl_usd"),
                "status": "settled",
                "entry_source": (f.get("meta") or {}).get("entry_source") or "",
            }
        )

    for f in fills.values():
        if f.get("signal_id") in settled_ids:
            continue
        fmeta = f.get("meta") or {}
        rows.append(
            {
                "time": f.get("filled_at") or "",
                "lane": label,
                "instance_id": instance_id,
                "slug": f.get("slug") or fmeta.get("slug") or "",
                "direction": f.get("direction"),
                "size": f.get("size_usd"),
                "entry": f.get("fill_price"),
                "exit": "—",
                "won": "open",
                "pnl": None,  # unrealized — never show $0 as if settled flat
                "status": "open",
                "entry_source": fmeta.get("entry_source") or "",
            }
        )

    rows.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
    return rows[:limit]


def fleet_trade_history(limit: int = 50) -> list[dict[str, Any]]:
    """Newest trades across all lanes (settled + open), capped at ``limit``."""
    rows: list[dict[str, Any]] = []
    for meta in INSTANCE_METAS:
        # Pull a buffer per lane so the fleet top-N is accurate
        rows.extend(instance_trade_history(meta["id"], limit=limit))
    rows.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
    return rows[:limit]


def trade_history_pnl_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Settled PnL totals for a trade-history view (open rows excluded)."""
    settled = [r for r in rows if r.get("status") == "settled"]
    open_n = sum(1 for r in rows if r.get("status") == "open")
    pnl = sum(float(r.get("pnl") or 0) for r in settled)
    return {
        "n_rows": len(rows),
        "n_settled": len(settled),
        "n_open": open_n,
        "settled_pnl": round(pnl, 2),
    }


def bandit_states_all() -> dict[str, Any]:
    """Per-instance bandit summaries keyed by instance_id."""
    out: dict[str, Any] = {}
    for meta in INSTANCE_METAS:
        iid = meta["id"]
        path = paper_dir() / iid / "bandit_state.json"
        if not path.is_file():
            out[iid] = {"pulls": 0, "explore_rate": 0.0}
            continue
        try:
            raw = json.loads(path.read_text())
            pulls = int(raw.get("global_pulls") or 0)
            explore = int(raw.get("global_explore") or 0)
            out[iid] = {
                "pulls": pulls,
                "explore_rate": explore / pulls if pulls else 0.0,
                "exploit": raw.get("global_exploit", 0),
                "explore": explore,
                "skip": raw.get("global_skip", 0),
            }
        except Exception as exc:  # noqa: BLE001
            out[iid] = {"error": str(exc)}
    return out


def load_trades() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in instance_paper_dirs():
        for r in read_jsonl(d / "trade_ledger.jsonl"):
            if isinstance(r, dict):
                r.setdefault("instance_id", d.name if d.name != "paper" else "legacy")
                rows.append(r)
    return rows


def load_pretrade() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in instance_paper_dirs():
        rows.extend(read_jsonl(d / "pretrade_decisions.jsonl"))
    return rows


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


def equity_curve(starting: float = FLEET_BANKROLL) -> list[dict[str, Any]]:
    """Fleet cumulative equity (default $10k start)."""
    return fleet_equity_curve(starting)


def total_pnl(starting: float = FLEET_BANKROLL) -> float:
    return fleet_total_pnl(starting)


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
    """Performance cards for each dedicated fast-crypto lane."""
    from hermes.market_scope import (
        SERIES_BTC_5M,
        SERIES_BTC_15M,
        SERIES_ETH_5M,
        SERIES_SOL_5M,
        preferred_slugs,
        record_belongs_to_series,
    )

    settles = load_settlements()
    pretrades = load_pretrade()
    cards = []
    for series, label in (
        (SERIES_BTC_15M, "BTC Up/Down 15m"),
        (SERIES_BTC_5M, "BTC Up/Down 5m"),
        (SERIES_ETH_5M, "ETH Up/Down 5m"),
        (SERIES_SOL_5M, "SOL Up/Down 5m"),
    ):
        rows = [s for s in settles if record_belongs_to_series(s, series)]
        pts = [p for p in pretrades if record_belongs_to_series(p, series)]
        wins = sum(1 for r in rows if r.get("won") or float(r.get("pnl_usd", 0)) > 0)
        pnls = [float(r.get("pnl_usd", 0)) for r in rows]
        sizes = [float(r.get("size_usd", 0) or 0) for r in rows]
        last_pt = pts[-1] if pts else {}
        asset = series.split("_")[0]
        pref = [
            s
            for s in preferred_slugs()
            if s.startswith(f"{asset}-updown-")
            and (
                ("-15m-" in s and series.endswith("15m"))
                or ("-5m-" in s and series.endswith("5m"))
            )
        ]
        open_rows = [
            f
            for f in load_fills()
            if record_belongs_to_series(f, series)
            and f.get("signal_id") not in {s.get("signal_id") for s in settles}
        ]
        cards.append(
            {
                "series": series,
                "label": label,
                "preferred_slug": pref[0] if pref else "",
                "n": len(rows),
                "wr": (wins / len(rows)) if rows else None,
                "pnl": sum(pnls) if pnls else 0.0,
                "avg_size": (sum(sizes) / len(sizes)) if sizes else None,
                "open_n": len(open_rows),
                "current_size_pct": last_pt.get("recommended_size_pct"),
                "current_size_usd": last_pt.get("recommended_size_usd"),
                "last_skip": last_pt.get("skip"),
                "last_reasons": last_pt.get("reasons") or [],
                "last_live_ev": last_pt.get("live_ev"),
                "last_slug": last_pt.get("slug") or (rows[-1].get("slug") if rows else ""),
            }
        )
    return cards


def scoped_lane_trade_history(series: str, limit: int = 50) -> list[dict[str, Any]]:
    """Last N settled + open trades for one lane, newest first."""
    from hermes.market_scope import record_belongs_to_series

    fills = {f.get("signal_id"): f for f in load_fills()}
    settled_ids = set()
    rows: list[dict[str, Any]] = []

    for s in load_settlements():
        if not record_belongs_to_series(s, series):
            continue
        sid = s.get("signal_id")
        if sid:
            settled_ids.add(sid)
        f = fills.get(sid, {})
        ts = (
            s.get("settled_at")
            or s.get("filled_at")
            or s.get("created_at")
            or ""
        )
        rows.append(
            {
                "time": ts,
                "market_id": s.get("market_id"),
                "slug": s.get("slug") or f.get("slug") or "",
                "direction": s.get("direction"),
                "entry": f.get("fill_price") or s.get("entry_price"),
                "exit": s.get("exit_price"),
                "won": s.get("won"),
                "pnl": s.get("pnl_usd"),
                "size": s.get("size_usd") or f.get("size_usd"),
                "sleeve": s.get("substrategy_id", ""),
                "entry_source": (f.get("meta") or {}).get("entry_source")
                or s.get("entry_source")
                or "",
                "bandit": (f.get("meta") or {}).get("bandit_arm") or "",
                "status": "settled",
                "reason": s.get("notes") or "",
            }
        )

    for f in load_fills():
        if not record_belongs_to_series(f, series):
            continue
        if f.get("signal_id") in settled_ids:
            continue
        meta = f.get("meta") or {}
        rows.append(
            {
                "time": f.get("filled_at") or "",
                "market_id": f.get("market_id"),
                "slug": f.get("slug") or meta.get("slug") or "",
                "direction": f.get("direction"),
                "entry": f.get("fill_price"),
                "exit": "—",
                "won": "open",
                "pnl": 0.0,
                "size": f.get("size_usd"),
                "sleeve": meta.get("substrategy_id") or f.get("substrategy_id", ""),
                "entry_source": meta.get("entry_source") or "",
                "bandit": meta.get("bandit_arm") or "",
                "status": "open",
                "reason": "open paper position",
            }
        )

    rows.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
    return rows[:limit]


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
    """Flatten fills + settlements across fleet (newest first)."""
    rows: list[dict[str, Any]] = []
    for meta in INSTANCE_METAS:
        iid = meta["id"]
        for r in instance_trade_history(iid, limit=limit):
            rows.append({**r, "instance": iid, "label": meta["label"]})
    rows.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
    return rows[:limit]
