#!/usr/bin/env python3
"""Pull VPS paper ledgers and publish a trailing-window trading report."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]

# Must match docker-compose 10-lane BTC15 experiment
INSTANCE_IDS = (
    "lane01_baseline",
    "lane02_chainlink",
    "lane03_favorite",
    "lane04_longshot",
    "lane05_late",
    "lane06_garch",
    "lane07_marketsigma",
    "lane08_legacy",
    "lane09_random",
    "lane10_depth",
)
STARTING_BANKROLL = 2000.0
FLEET_BANKROLL = STARTING_BANKROLL * len(INSTANCE_IDS)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except json.JSONDecodeError:
            continue
    return rows


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def in_window(ts: Any, cutoff: datetime) -> bool:
    dt = parse_ts(ts)
    return bool(dt and dt >= cutoff)


def rsync_from_vps(dest: Path, host: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    key = Path.home() / ".ssh" / "bot3_cloud_agent"
    ssh = f"ssh -i {key} -o StrictHostKeyChecking=no -o BatchMode=yes"
    cmd = [
        "rsync",
        "-avz",
        "-e",
        ssh,
        f"root@{host}:/opt/financial-freedom-bot/data/paper/",
        str(dest) + "/",
    ]
    subprocess.run(cmd, check=True)


def trade_rows(paper_dir: Path, instance_id: str) -> list[dict[str, Any]]:
    ledger = read_jsonl(paper_dir / instance_id / "trade_ledger.jsonl")
    fills = {f.get("signal_id"): f for f in ledger if f.get("event") == "fill"}
    rows: list[dict[str, Any]] = []
    for s in ledger:
        if s.get("event") != "settlement":
            continue
        sid = s.get("signal_id")
        f = fills.get(sid, {})
        meta = f.get("meta") or s.get("meta") or {}
        rows.append(
            {
                "instance_id": instance_id,
                "signal_id": sid,
                "slug": s.get("slug") or f.get("slug") or "",
                "direction": s.get("direction") or f.get("direction"),
                "size_usd": s.get("size_usd") or f.get("size_usd"),
                "entry_price": s.get("entry_price") or f.get("fill_price"),
                "pnl_usd": float(s.get("pnl_usd") or 0),
                "won": s.get("won"),
                "settled_at": s.get("settled_at"),
                "entry_source": meta.get("entry_source") or "",
                "model_q_source": meta.get("model_q_source") or "",
                "model_q": meta.get("model_q"),
                "live_real_q": meta.get("live_real_q"),
                "enhanced_edge": meta.get("enhanced_edge"),
                "bandit_arm": meta.get("bandit_arm"),
            }
        )
    rows.sort(key=lambda r: str(r.get("settled_at") or ""))
    return rows


def instance_window_stats(
    paper_dir: Path, instance_id: str, cutoff: datetime
) -> dict[str, Any]:
    ledger = read_jsonl(paper_dir / instance_id / "trade_ledger.jsonl")
    pts = read_jsonl(paper_dir / instance_id / "pretrade_decisions.jsonl")
    turns = read_jsonl(paper_dir / instance_id / "turns.jsonl")
    fills = [r for r in ledger if r.get("event") == "fill"]
    settles = [r for r in ledger if r.get("event") == "settlement"]

    life_pnls = [float(s.get("pnl_usd") or 0) for s in settles]
    life_pnl = sum(life_pnls)
    equity = STARTING_BANKROLL + life_pnl

    win_settles = [s for s in settles if in_window(s.get("settled_at"), cutoff)]
    win_fills = [
        f
        for f in fills
        if in_window(f.get("filled_at") or f.get("ts") or f.get("created_at"), cutoff)
    ]
    win_pnls = [float(s.get("pnl_usd") or 0) for s in win_settles]
    wins = sum(1 for s in win_settles if s.get("won") or float(s.get("pnl_usd") or 0) > 0)
    losses = len(win_settles) - wins

    settled_ids = {s.get("signal_id") for s in settles if s.get("signal_id")}
    open_n = sum(1 for f in fills if f.get("signal_id") and f.get("signal_id") not in settled_ids)

    turns_in = [
        t
        for t in turns
        if in_window(t.get("finished_at") or t.get("started_at") or t.get("ts"), cutoff)
    ]
    orders = sum(int(t.get("orders_sent") or t.get("fills") or 0) for t in turns_in)
    signals = sum(int(t.get("signals") or t.get("signals_generated") or 0) for t in turns_in)
    last = turns[-1] if turns else {}
    last_turn = ""
    if last:
        last_turn = (
            f"turn={last.get('turn_id') or last.get('turn') or '?'} "
            f"candidates={last.get('candidates', '?')} "
            f"signals={last.get('signals', last.get('signals_generated', '?'))} "
            f"pass={last.get('pass', last.get('enhanced_pass', '?'))} "
            f"reject={last.get('reject', '?')} "
            f"fills={last.get('fills', '?')} "
            f"lessons={last.get('lessons', '?')}"
        )

    return {
        "instance_id": instance_id,
        "bankroll_usd": STARTING_BANKROLL,
        "window_settled": len(win_settles),
        "window_wins": wins,
        "window_losses": losses,
        "window_wr": round(wins / len(win_settles), 4) if win_settles else None,
        "window_pnl_usd": round(sum(win_pnls), 2),
        "window_fills": len(win_fills),
        "lifetime_settled": len(settles),
        "lifetime_pnl_usd": round(life_pnl, 2),
        "equity_usd": round(equity, 2),
        "open_positions": open_n,
        "pretrade_total": len(pts),
        "turns_in_window": len(turns_in),
        "orders_sent": orders,
        "signals_generated": signals,
        "last_turn": last_turn,
        "last_finished_at": last.get("finished_at") or last.get("ts") or "",
    }


def fmt_pct(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(v)


def build_report(
    paper_dir: Path,
    *,
    hours: float,
    vps_host: str,
    main_commit: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    instances = [instance_window_stats(paper_dir, iid, cutoff) for iid in INSTANCE_IDS]
    trades: list[dict[str, Any]] = []
    for iid in INSTANCE_IDS:
        for row in trade_rows(paper_dir, iid):
            if in_window(row.get("settled_at"), cutoff):
                trades.append(row)
    trades.sort(key=lambda r: str(r.get("settled_at") or ""))

    window_settled = sum(i["window_settled"] for i in instances)
    window_wins = sum(i["window_wins"] for i in instances)
    window_losses = sum(i["window_losses"] for i in instances)
    window_pnl = sum(i["window_pnl_usd"] for i in instances)
    fleet_equity = sum(i["equity_usd"] for i in instances)

    section = {
        "cutoff_utc": cutoff.isoformat(),
        "window_hours": hours,
        "fleet_bankroll_usd": FLEET_BANKROLL,
        "fleet_equity_usd": round(fleet_equity, 2),
        "fleet_lifetime_pnl_usd": round(fleet_equity - FLEET_BANKROLL, 2),
        "window_pnl_usd": round(window_pnl, 2),
        "window_settled": window_settled,
        "window_wins": window_wins,
        "window_losses": window_losses,
        "window_wr": round(window_wins / window_settled, 4) if window_settled else None,
        "instances": instances,
    }
    return {
        "window": f"last_{int(hours)}_hours" if hours == int(hours) else f"last_{hours}_hours",
        "cutoff_utc": cutoff.isoformat(),
        "generated_at": now.isoformat(),
        "vps_host": vps_host,
        "main_commit": main_commit,
        "section_a_last_3h": section,
        "trades": trades,
    }


def render_text(report: dict[str, Any]) -> str:
    s = report["section_a_last_3h"]
    hours = s["window_hours"]
    label = f"last {int(hours)} hours" if hours == int(hours) else f"last {hours} hours"
    lines = [
        f"Bot 3 — Full Trading Report ({label})",
        f"Generated: {report['generated_at']}",
        f"Window start (UTC): {report['cutoff_utc']}",
        f"VPS: {report['vps_host']} | main @ {report['main_commit']}",
        "",
        f"=== A) Live paper fleet — {label} ===",
        f"Settled trades: {s['window_settled']} | W/L: {s['window_wins']}/{s['window_losses']} | "
        f"WR: {fmt_pct(s.get('window_wr'))}",
        f"Window PnL: ${s['window_pnl_usd']:+,.2f}",
        f"Fleet equity (lifetime): ${s['fleet_equity_usd']:,.2f} / ${s['fleet_bankroll_usd']:,.2f} bankroll",
        f"Fleet lifetime PnL: ${s['fleet_lifetime_pnl_usd']:+,.2f}",
        "",
    ]
    for inst in s["instances"]:
        wr = fmt_pct(inst.get("window_wr"))
        lines.append(
            f"  {inst['instance_id']:22}  window_settled={inst['window_settled']:3}  "
            f"pnl=${inst['window_pnl_usd']:+8.2f}  wr={wr:6}  "
            f"equity=${inst['equity_usd']:,.2f}  turns={inst['turns_in_window']}  "
            f"orders={inst['orders_sent']}"
        )
    lines.extend(["", f"=== Settled trades ({label}) ==="])
    if not report["trades"]:
        lines.append("  (none)")
    for t in report["trades"]:
        q = t.get("live_real_q")
        q_txt = f"{q}" if q is not None else "?"
        lines.append(
            f"  {t.get('settled_at')}  {t['instance_id']:22}  "
            f"{str(t.get('direction') or '?'):4}  pnl=${float(t.get('pnl_usd') or 0):+8.2f}  "
            f"won={t.get('won')}  size=${t.get('size_usd')}  q={q_txt}  "
            f"src={t.get('entry_source') or t.get('model_q_source') or '?'}  "
            f"{t.get('slug')}"
        )
    lines.extend(
        [
            "",
            "=== Notes ===",
            "Source: VPS data/paper/* ledgers (rsync read-only pull).",
            f"Window filter: settlement.settled_at >= now-{hours}h UTC.",
            "Equity/lifetime PnL include all settlements since last fleet reset.",
            f"Fleet: {len(INSTANCE_IDS)}× BTC15 lanes × ${STARTING_BANKROLL:,.0f} = ${FLEET_BANKROLL:,.0f}.",
        ]
    )
    return "\n".join(lines)


def render_readme(report: dict[str, Any]) -> str:
    s = report["section_a_last_3h"]
    hours = s["window_hours"]
    label = f"last {int(hours)} hours" if hours == int(hours) else f"last {hours} hours"
    day = report["generated_at"][:10]
    rows = "\n".join(
        f"| {i['instance_id']} | {i['window_settled']} | ${i['window_pnl_usd']:+,.2f} | "
        f"{fmt_pct(i.get('window_wr'))} | ${i['equity_usd']:,.2f} | "
        f"{i['turns_in_window']} | {i['orders_sent']} |"
        for i in s["instances"]
    )
    return f"""# Full Trading Report — {label} ({day})

Pulled from VPS paper fleet (`{report['vps_host']}`). Window: settlements with `settled_at` ≥ `{report['cutoff_utc']}`.

## Summary

| Metric | Value |
|--------|-------|
| Window settled | {s['window_settled']} |
| Window W/L | {s['window_wins']} / {s['window_losses']} |
| Window WR | {fmt_pct(s.get('window_wr'))} |
| Window PnL | ${s['window_pnl_usd']:+,.2f} |
| Fleet equity (lifetime) | ${s['fleet_equity_usd']:,.2f} / ${s['fleet_bankroll_usd']:,.0f} |
| Fleet lifetime PnL | ${s['fleet_lifetime_pnl_usd']:+,.2f} |
| Commit at pull | `{report['main_commit']}` |

## Per lane (window)

| Lane | Settled | PnL | WR | Equity | Turns | Orders |
|------|---------|-----|----|--------|-------|--------|
{rows}

## Files

| File | Purpose |
|------|---------|
| `report.txt` | Human-readable summary |
| `report.json` | Full structured report |
| `fleet_paper.json` | Fleet + lane stats |
| `trades.json` | Settled trades in the {label} window |
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=3.0)
    ap.add_argument("--paper-dir", type=Path, default=ROOT / "data" / "paper_pull")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--vps-host", default="207.246.96.45")
    args = ap.parse_args(argv)

    if args.pull:
        rsync_from_vps(args.paper_dir, args.vps_host)

    commit = "unknown"
    try:
        commit = (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT)
            .decode()
            .strip()
        )
    except subprocess.CalledProcessError:
        pass

    report = build_report(
        args.paper_dir,
        hours=args.hours,
        vps_host=args.vps_host,
        main_commit=commit,
    )
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    hours_tag = f"last{int(args.hours)}h" if args.hours == int(args.hours) else f"last{args.hours}h"
    out = args.out_dir or (ROOT / "reports" / f"full_trading_report_{hours_tag}_{day}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "fleet_paper.json").write_text(
        json.dumps(report["section_a_last_3h"], indent=2) + "\n"
    )
    (out / "trades.json").write_text(json.dumps(report["trades"], indent=2) + "\n")
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "report.txt").write_text(render_text(report) + "\n")
    (out / "README.md").write_text(render_readme(report) + "\n")
    print(render_text(report))
    print(f"\nWrote bundle → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
