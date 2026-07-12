#!/usr/bin/env python3
"""Monte Carlo profit discovery for Polymarket BTC/ETH 4h markets.

Reuses the same bot feed (price, sigma, TV FIFOs, signal learning, council) as the
15m/1h discovery, but sweeps 4h (14400s) windows — the series the bot already
knows as btc-up-or-down-4h / eth-up-or-down-4h.

Usage:
  python scripts/mc_profit_discovery/run_discovery_4h.py \\
      --status /tmp/vps_status.json --tv /tmp/vps_tv.json \\
      --out /tmp/mc_profit_out_4h --n-paths 1000000
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from run_discovery import (  # noqa: E402
    _bar_close_rows,
    _div_lean,
    _rsi_band_lean,
    _short_lean,
    erf_vec,
    lean_mu_tilt,
    pnl_binary,
    prepare_mc_feed,
    simulate_window_paths,
)


def enrich_feed_for_4h(feed: dict, tv: dict) -> dict:
    """Add 4h lane charts: regime lean from *USDT (more history) + INDEX for settlement."""
    charts = dict(feed.get("lane_charts") or {})
    for asset, usdt, index in (
        ("btc", "BTCUSDT", "BTCUSD"),
        ("eth", "ETHUSDT", "ETHUSD"),
    ):
        bars_u = _bar_close_rows(tv, usdt)
        bars_i = _bar_close_rows(tv, index)
        # 4h ≈ 48 × 5m bars; use all available up to 50 as regime
        charts[f"{asset}_4h"] = {
            "symbol": usdt,
            "settlement_symbol": index,
            "bar_close_n": len(bars_u),
            "short_lean": _short_lean(bars_u, 12),   # last ~1h of 5m
            "regime_lean": _short_lean(bars_u, 48),  # last ~4h of 5m
            "index_regime_lean": _short_lean(bars_i, 48) if bars_i else {},
            "rsi_band": _rsi_band_lean(tv, usdt),
            "rsi_divergence": _div_lean(tv, usdt),
            "note": "4h lane: USDT regime for lead; INDEX for settlement alignment",
        }
    feed = dict(feed)
    feed["lane_charts"] = charts
    feed["windows"] = {
        **(feed.get("windows") or {}),
        "4h": {"window_seconds": 14400, "lanes": ["btc_4h", "eth_4h"]},
    }
    feed["lane_routing"] = {
        **(feed.get("lane_routing") or {}),
        "4h": "binance_usdt lead (*USDT) + chainlink index settle (*USD)",
    }
    feed["schema"] = "mc_profit_discovery_feed/1.1-4h"
    # Council priors for 4h
    council = feed.get("council") or {}
    feed["scenario_priors"] = {
        **(feed.get("scenario_priors") or {}),
        "tv_2h_accuracy": (council.get("tv_2h_trend") or {}).get("accuracy") or 0.60,
        "tv_240m_accuracy": (council.get("tv_240m") or {}).get("accuracy") or 0.25,
        "note": "4h: prefer tv_2h follow; tv_240m historically anti-predictive → fade candidate",
    }
    return feed


def run_profit_discovery_4h(feed: dict, *, n_paths: int = 1_000_000, seed: int = 42) -> dict:
    rng = np.random.default_rng(seed)
    size = float((feed.get("execution_assumptions") or {}).get("size_usd") or 5.0)
    slip = float((feed.get("execution_assumptions") or {}).get("slippage_abs") or 0.01)
    min_entry = float((feed.get("execution_assumptions") or {}).get("min_entry_price") or 0.50)
    max_entry = float((feed.get("execution_assumptions") or {}).get("max_entry_price") or 0.75)

    assets = feed["assets"]
    charts = feed["lane_charts"]
    priors = feed.get("scenario_priors") or {}
    tv2h_acc = float(priors.get("tv_2h_accuracy") or 0.60)
    tv240_acc = float(priors.get("tv_240m_accuracy") or 0.25)
    streak_fade = float(priors.get("streak3_fade_rate") or 0.75)

    # 4h TTC grid (seconds remaining)
    ttc_grid = [10800, 7200, 5400, 3600, 2700, 1800, 900, 600, 300]
    ask_grid = [0.50, 0.52, 0.55, 0.58, 0.60, 0.65, 0.70]
    sides = ("up", "down")
    lean_modes = (
        "neutral",
        "follow_regime",      # 4h of 5m bars
        "fade_regime",
        "follow_short_1h",    # last ~1h of 5m
        "follow_tv2h_edge",
        "fade_tv240",         # 240m council is anti-predictive
        "fade_streak3",
        "up_weak_only",       # only tilt when we'd treat signal as UP_WEAK-quality
    )

    scenarios = []
    for asset in ("btc", "eth"):
        lane_key = f"{asset}_4h"
        scenarios.append({
            "asset": asset,
            "window": "4h",
            "window_s": 14400,
            "lane_key": lane_key,
            "ttc_grid": ttc_grid,
            "chart": charts.get(lane_key) or {},
            "s_now": float(assets[asset]["s_now"] or 0),
            "sigma": float(assets[asset]["sigma_per_sec"] or 0),
        })

    n_scen = len(scenarios)
    paths_per = int(n_paths // n_scen)
    actual_total = paths_per * n_scen
    n_steps = 48  # 5m steps across 4h

    agg = defaultdict(lambda: {"pnl_sum": 0.0, "n": 0, "wins": 0.0, "paths": 0})
    t0 = time.time()

    for scen in scenarios:
        delta = (scen["chart"].get("regime_lean") or {}).get("delta_pct") or 0.0
        s_open = float(scen["s_now"])
        if abs(float(delta)) < 8:
            s_open = s_open / (1.0 + float(delta) / 100.0)
        sigma = max(1e-7, float(scen["sigma"]))
        window_s = float(scen["window_s"])
        regime = scen["chart"].get("regime_lean") or {}
        short = scen["chart"].get("short_lean") or {}
        regime_lean = regime.get("lean")
        short_lean = short.get("lean")
        streak_len = int(short.get("streak_len") or 0)
        streak_dir = short.get("streak_dir")

        for lean_mode in lean_modes:
            if lean_mode == "neutral":
                mu = 0.0
            elif lean_mode == "follow_regime":
                mu = lean_mu_tilt(regime_lean, edge=0.05, sigma=sigma, ttc=window_s * 0.5)
            elif lean_mode == "fade_regime":
                opp = "down" if regime_lean == "up" else ("up" if regime_lean == "down" else None)
                mu = lean_mu_tilt(opp, edge=0.04, sigma=sigma, ttc=window_s * 0.5)
            elif lean_mode == "follow_short_1h":
                mu = lean_mu_tilt(short_lean, edge=0.04, sigma=sigma, ttc=1800.0)
            elif lean_mode == "follow_tv2h_edge":
                edge = max(0.0, tv2h_acc - 0.5)
                mu = lean_mu_tilt(regime_lean or short_lean or "up", edge=edge, sigma=sigma,
                                 ttc=window_s * 0.5)
            elif lean_mode == "fade_tv240":
                # 240m acc ~0.25 → fade its implied direction (= follow opposite of regime if weak)
                edge = max(0.0, 0.5 - tv240_acc)
                opp = "down" if (regime_lean or short_lean) == "up" else (
                    "up" if (regime_lean or short_lean) == "down" else None)
                mu = lean_mu_tilt(opp, edge=edge, sigma=sigma, ttc=window_s * 0.5)
            elif lean_mode == "fade_streak3":
                if streak_len >= 3 and streak_dir in ("up", "down"):
                    opp = "down" if streak_dir == "up" else "up"
                    mu = lean_mu_tilt(opp, edge=streak_fade - 0.5, sigma=sigma, ttc=600.0)
                else:
                    mu = 0.0
            elif lean_mode == "up_weak_only":
                # Historical UP_WEAK edge +0.136 — only apply bullish tilt
                mu = lean_mu_tilt("up", edge=0.136, sigma=sigma, ttc=window_s * 0.5)
            else:
                mu = 0.0

            paths = simulate_window_paths(
                s_open=s_open, sigma=sigma, window_s=window_s, n_paths=paths_per,
                mu=mu, n_steps=n_steps, rng=rng,
            )
            s_close = paths[:, -1]
            outcome_up = s_close >= s_open

            for ttc in scen["ttc_grid"]:
                if ttc >= window_s:
                    continue
                elapsed = window_s - float(ttc)
                step_i = int(round(elapsed / window_s * n_steps))
                step_i = max(0, min(n_steps, step_i))
                s_t = paths[:, step_i]
                sig_h = sigma * math.sqrt(float(ttc))
                if sig_h <= 0:
                    continue
                z = (np.log(np.maximum(s_t, 1e-12) / s_open) + (mu - 0.5 * sigma * sigma) * ttc) / sig_h
                p_model = 0.5 * (1.0 + erf_vec(z / math.sqrt(2.0)))

                for side in sides:
                    # up_weak_only: only evaluate UP side
                    if lean_mode == "up_weak_only" and side != "up":
                        continue
                    p_side = p_model if side == "up" else (1.0 - p_model)
                    win = outcome_up if side == "up" else ~outcome_up

                    for ask in ask_grid:
                        if ask < min_entry or ask > max_entry:
                            continue
                        edge = p_side - ask
                        for gate_name, mask in (
                            ("always", np.ones(paths_per, dtype=bool)),
                            ("edge_ge_0.02", edge >= 0.02),
                            ("edge_ge_0.05", edge >= 0.05),
                            ("edge_ge_0.08", edge >= 0.08),
                            ("p_ge_0.55", p_side >= 0.55),
                            ("p_ge_0.60", p_side >= 0.60),
                        ):
                            if not np.any(mask):
                                continue
                            pnl = pnl_binary(side, win[mask], ask, size, slippage=slip)
                            key = (
                                f"{scen['asset']}|4h|ttc{int(ttc)}|{side}|ask{ask:.2f}|"
                                f"{lean_mode}|{gate_name}"
                            )
                            a = agg[key]
                            a["pnl_sum"] += float(np.sum(pnl))
                            a["n"] += int(pnl.size)
                            a["wins"] += float(np.sum(win[mask]))
                            a["paths"] += int(mask.sum())
                            a["meta"] = {
                                "asset": scen["asset"],
                                "window": "4h",
                                "ttc_s": int(ttc),
                                "side": side,
                                "ask": ask,
                                "lean_mode": lean_mode,
                                "gate": gate_name,
                                "symbol": (scen["chart"] or {}).get("symbol"),
                                "sigma": sigma,
                                "mu": mu,
                            }

    elapsed = time.time() - t0
    ranked = []
    for key, a in agg.items():
        n = int(a["n"])
        if n < 500:
            continue
        pnl = float(a["pnl_sum"])
        wr = float(a["wins"]) / n
        ev = pnl / n
        ranked.append({
            "key": key,
            **a["meta"],
            "n_trades": n,
            "win_rate": round(wr, 4),
            "total_pnl_usd": round(pnl, 2),
            "ev_per_trade_usd": round(ev, 4),
        })
    ranked.sort(key=lambda r: (-r["ev_per_trade_usd"], -r["total_pnl_usd"], -r["win_rate"]))

    best_by_asset = {}
    for r in ranked:
        if r["asset"] not in best_by_asset and r["ev_per_trade_usd"] > 0 and r["win_rate"] >= 0.52:
            best_by_asset[r["asset"]] = r

    def summarize(field: str, top_n: int = 10):
        buckets = defaultdict(lambda: {"pnl": 0.0, "n": 0, "wins": 0.0})
        for r in ranked:
            b = buckets[r[field]]
            b["pnl"] += r["total_pnl_usd"]
            b["n"] += r["n_trades"]
            b["wins"] += r["win_rate"] * r["n_trades"]
        out = []
        for k, b in buckets.items():
            out.append({
                field: k,
                "n_trades": b["n"],
                "win_rate": round(b["wins"] / b["n"], 4) if b["n"] else None,
                "total_pnl_usd": round(b["pnl"], 2),
                "ev_per_trade_usd": round(b["pnl"] / b["n"], 4) if b["n"] else None,
            })
        out.sort(key=lambda x: -(x["ev_per_trade_usd"] or -999))
        return out[:top_n]

    # Honest: TV signals vs fair ask 0.55 on 4h horizon
    honest = _honest_4h_tv_alpha(feed, n_paths=min(200_000, n_paths // 2), seed=seed + 1)

    return {
        "schema": "mc_profit_discovery_result/1.0-4h",
        "market": "polymarket_btc_eth_up_or_down_4h",
        "window_seconds": 14400,
        "n_paths_requested": n_paths,
        "n_paths_simulated": actual_total,
        "paths_per_scenario": paths_per,
        "n_scenarios": n_scen,
        "n_policies_ranked": len(ranked),
        "elapsed_s": round(elapsed, 2),
        "seed": seed,
        "size_usd": size,
        "slippage_abs": slip,
        "top_20_policies": ranked[:20],
        "best_positive_by_asset": best_by_asset,
        "summary_by_lean_mode": summarize("lean_mode"),
        "summary_by_gate": summarize("gate"),
        "summary_by_side": summarize("side"),
        "summary_by_asset": summarize("asset"),
        "summary_by_ttc": summarize("ttc_s"),
        "honest_tv_alpha": honest,
        "recommendation": _rec_4h(ranked, best_by_asset, feed, honest),
    }


def _honest_4h_tv_alpha(feed: dict, *, n_paths: int, seed: int) -> dict:
    """Fair ask 0.55 + slip: inject hist signal edge into 4h path drift."""
    rng = np.random.default_rng(seed)
    size, slip = 5.0, 0.01
    ask = 0.55
    q = ask + slip
    sl = (feed.get("tv_signal_learning") or {}).get("by_signal_level") or {}
    signals = []
    for lvl, meta in sl.items():
        wr = meta.get("win_rate")
        n = int(meta.get("n") or 0)
        if wr is None or n < 8:
            continue
        signals.append((lvl, float(wr) - 0.5))

    # Also test council-based tilts
    priors = feed.get("scenario_priors") or {}
    signals.append(("TV2H_FOLLOW", float(priors.get("tv_2h_accuracy") or 0.60) - 0.5))
    signals.append(("TV240_FADE", 0.5 - float(priors.get("tv_240m_accuracy") or 0.25)))

    rows = []
    per = max(5000, n_paths // max(1, len(signals) * 2))
    window_s = 14400.0
    for asset in ("btc", "eth"):
        a = feed["assets"][asset]
        s0 = float(a["s_now"])
        sig = float(a["sigma_per_sec"])
        for signal, edge in signals:
            shift = abs(edge) * sig * math.sqrt(window_s * 0.5)
            if signal.startswith("DOWN") or signal == "BAR_BEAR":
                side = "down"
                mu = -abs(shift / (window_s * 0.5)) * (1 if edge >= 0 else -1)
            elif signal == "TV240_FADE":
                # fade = trade opposite of a weak 240m; use down as default fade of bullish bias
                side = "down"
                mu = -abs(shift / (window_s * 0.5))
            else:
                side = "up"
                mu = abs(shift / (window_s * 0.5)) * (1 if edge >= 0 else -1)
            paths = simulate_window_paths(
                s_open=s0, sigma=sig, window_s=window_s, n_paths=per, mu=mu, n_steps=48, rng=rng)
            up = paths[:, -1] >= s0
            win = up if side == "up" else ~up
            w = win.astype(float)
            pnl = size * (w * (1 / q - 1) + (1 - w) * (-1))
            rows.append({
                "asset": asset,
                "signal": signal,
                "hist_edge": round(edge, 4),
                "side": side,
                "n": int(w.size),
                "win_rate": round(float(w.mean()), 4),
                "ev_per_trade_usd": round(float(pnl.mean()), 4),
                "total_pnl_usd": round(float(pnl.sum()), 2),
            })
    rows.sort(key=lambda r: -r["ev_per_trade_usd"])
    # Aggregate by signal across assets
    by = defaultdict(list)
    for r in rows:
        by[r["signal"]].append(r)
    summary = []
    for sig, rs in by.items():
        summary.append({
            "signal": sig,
            "hist_edge": rs[0]["hist_edge"],
            "mean_wr": round(float(np.mean([r["win_rate"] for r in rs])), 4),
            "mean_ev": round(float(np.mean([r["ev_per_trade_usd"] for r in rs])), 4),
            "n_paths": sum(r["n"] for r in rs),
            "verdict": "USE" if np.mean([r["ev_per_trade_usd"] for r in rs]) > 0 else "AVOID",
        })
    summary.sort(key=lambda x: -x["mean_ev"])
    return {"ask": ask, "slippage": slip, "by_signal": summary, "detail": rows}


def _rec_4h(ranked, best_by_asset, feed, honest) -> dict:
    top = [r for r in ranked if r["ev_per_trade_usd"] > 0 and r["win_rate"] >= 0.55][:5]
    use = [h for h in (honest.get("by_signal") or []) if h.get("verdict") == "USE"]
    avoid = [h for h in (honest.get("by_signal") or []) if h.get("verdict") == "AVOID"]
    always = next((r for r in ranked if r.get("gate") == "always" and r.get("lean_mode") == "neutral"), None)
    return {
        "market": "btc/eth-up-or-down-4h",
        "edge_found": bool(use),
        "honest_use_signals": use,
        "honest_avoid_signals": avoid[:6],
        "best_sweep_policies": top,
        "best_by_asset": best_by_asset,
        "always_neutral_baseline": always,
        "symbols": {
            "btc_4h": "BTCUSDT (lead) / BTCUSD (settle)",
            "eth_4h": "ETHUSDT (lead) / ETHUSD (settle)",
        },
        "note": (
            "Honest edge = TV hist tilt vs fair ask 0.55. Sweep edge_ge_* rows need real "
            "book mispricing. tv_240m is anti-predictive — fade, don't follow."
        ),
    }


def _to_markdown(feed: dict, result: dict) -> str:
    lines = [
        "# Monte Carlo Profit Discovery — Polymarket BTC/ETH 4h",
        "",
        f"_Prepared {feed.get('prepared_at_utc')} · PAPER ONLY · {result['n_paths_simulated']:,} paths_",
        "",
        "## Bot feed (same inputs as 15m/1h sim)",
        "",
        f"| Asset | Spot | sigma/s |",
        f"|-------|------:|--------:|",
        f"| BTC | {feed['assets']['btc']['s_now']} | {feed['assets']['btc']['sigma_per_sec']} |",
        f"| ETH | {feed['assets']['eth']['s_now']} | {feed['assets']['eth']['sigma_per_sec']} |",
        "",
    ]
    for k in ("btc_4h", "eth_4h"):
        v = (feed.get("lane_charts") or {}).get(k) or {}
        rg = v.get("regime_lean") or {}
        sh = v.get("short_lean") or {}
        lines.append(
            f"- **{k}** `{v.get('symbol')}`: regime_lean={rg.get('lean')} "
            f"δ={rg.get('delta_pct')} short_1h={sh.get('lean')} bars={v.get('bar_close_n')}"
        )
    lines += [
        "",
        f"Council: tv_2h={(feed.get('council') or {}).get('tv_2h_trend', {}).get('accuracy')} "
        f"tv_240m={(feed.get('council') or {}).get('tv_240m', {}).get('accuracy')}",
        "",
        "## Honest TV alpha vs fair ask 0.55 (+1¢ slip) — THE EDGE TEST",
        "",
        "| Signal | Hist edge | Sim WR | EV $/trade | Verdict |",
        "|--------|----------:|-------:|-----------:|---------|",
    ]
    for r in (result.get("honest_tv_alpha") or {}).get("by_signal") or []:
        lines.append(
            f"| {r['signal']} | {r['hist_edge']:+.3f} | {r['mean_wr']:.3f} | "
            f"{r['mean_ev']:.4f} | **{r['verdict']}** |"
        )
    lines += ["", "## Gate summary (1M sweep)", ""]
    for r in result.get("summary_by_gate") or []:
        lines.append(f"- `{r['gate']}`: WR={r['win_rate']} EV=${r['ev_per_trade_usd']}")
    lines += ["", "## Lean mode summary", ""]
    for r in result.get("summary_by_lean_mode") or []:
        lines.append(f"- `{r['lean_mode']}`: WR={r['win_rate']} EV=${r['ev_per_trade_usd']}")
    lines += ["", "## Top 10 sweep policies (caveat: need real book edge)", ""]
    for i, r in enumerate((result.get("top_20_policies") or [])[:10], 1):
        lines.append(
            f"{i}. {r['asset']} ttc={r['ttc_s']} {r['side']} ask={r['ask']} "
            f"{r['lean_mode']}/{r['gate']} WR={r['win_rate']} EV=${r['ev_per_trade_usd']}"
        )
    lines += ["", "## Recommendation", "", "```json",
              json.dumps(result.get("recommendation"), indent=2), "```", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True)
    ap.add_argument("--tv", required=True)
    ap.add_argument("--light", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-paths", type=int, default=1_000_000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    status = json.loads(Path(args.status).read_text())
    tv = json.loads(Path(args.tv).read_text())
    light = json.loads(Path(args.light).read_text()) if args.light else {}

    print("Preparing MC feed + 4h lane enrichment...")
    feed = enrich_feed_for_4h(prepare_mc_feed(status, tv, light=light), tv)
    feed_path = out_dir / "mc_feed_4h.json"
    feed_path.write_text(json.dumps(feed, indent=2, default=str))
    print(f"  wrote {feed_path}")
    for k in ("btc_4h", "eth_4h"):
        v = feed["lane_charts"][k]
        rg = v.get("regime_lean") or {}
        print(f"  {k}: {v['symbol']} regime={rg.get('lean')} δ={rg.get('delta_pct')} bars={v['bar_close_n']}")

    print(f"\nRunning 4h profit discovery: {args.n_paths:,} paths...")
    result = run_profit_discovery_4h(feed, n_paths=args.n_paths, seed=args.seed)
    result_path = out_dir / "mc_profit_discovery_4h_result.json"
    result_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"  wrote {result_path}")
    print(f"  simulated {result['n_paths_simulated']:,} in {result['elapsed_s']}s")
    print(f"  ranked {result['n_policies_ranked']} policies")

    print("\n=== HONEST TV ALPHA (fair ask 0.55) — EDGE FINDER ===")
    for r in (result.get("honest_tv_alpha") or {}).get("by_signal") or []:
        print(f"  {r['signal']:14} edge={r['hist_edge']:+.3f} WR={r['mean_wr']:.3f} "
              f"EV=${r['mean_ev']:.4f} [{r['verdict']}]")

    print("\n=== TOP 10 SWEEP POLICIES ===")
    for i, r in enumerate(result["top_20_policies"][:10], 1):
        print(
            f"  {i:2}. {r['asset']:3} ttc={r['ttc_s']:5} {r['side']:4} ask={r['ask']:.2f} "
            f"lean={r['lean_mode']:16} gate={r['gate']:12} "
            f"WR={r['win_rate']:.3f} EV=${r['ev_per_trade_usd']:.4f}"
        )

    print("\n=== SUMMARY BY LEAN / GATE ===")
    for r in result.get("summary_by_lean_mode") or []:
        print(f"  lean {r['lean_mode']:16} WR={r['win_rate']} EV=${r['ev_per_trade_usd']}")
    for r in result.get("summary_by_gate") or []:
        print(f"  gate {r['gate']:12} WR={r['win_rate']} EV=${r['ev_per_trade_usd']}")

    rec = result.get("recommendation") or {}
    print("\n=== EDGE FOUND? ===", rec.get("edge_found"))
    print(json.dumps({"use": rec.get("honest_use_signals"), "symbols": rec.get("symbols")}, indent=2))

    md_path = out_dir / "MC_PROFIT_DISCOVERY_4H.md"
    md_path.write_text(_to_markdown(feed, result))
    print(f"\nWrote {md_path}")


if __name__ == "__main__":
    main()
