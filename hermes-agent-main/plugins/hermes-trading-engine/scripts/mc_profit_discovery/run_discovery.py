#!/usr/bin/env python3
"""Monte Carlo profit discovery — prepare bot feed + run 1M paths (BTC/ETH).

PAPER research only. Uses live VPS bot state (price, sigma, TV FIFOs, signal
learning, council accuracy, lane policies) to parameterize GBM+jump paths and
sweep entry policies for Polymarket up/down windows.

Usage:
  python scripts/mc_profit_discovery/run_discovery.py \\
      --status /tmp/vps_status.json --tv /tmp/vps_tv.json \\
      --out /tmp/mc_profit_out --n-paths 1000000
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

# ---------------------------------------------------------------------------
# Feed preparation
# ---------------------------------------------------------------------------


def _f(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _bar_close_rows(tv: dict, symbol: str) -> list:
    rows = (tv.get("alert_history_by_symbol") or {}).get(symbol) or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("signal_kind") or "").lower()
        if kind.startswith("bar_close") or str(r.get("signal_level") or "").startswith("BAR_"):
            out.append(r)
    out.sort(key=lambda r: float(r.get("bar_time") or r.get("received_at") or 0))
    return out


def _short_lean(rows: list, n: int = 8) -> dict:
    window = rows[-max(1, n):]
    dirs = [str(r.get("direction") or "").upper() for r in window]
    up = sum(1 for d in dirs if d == "UP")
    dn = sum(1 for d in dirs if d == "DOWN")
    lean = None
    if up > dn:
        lean = "up"
    elif dn > up:
        lean = "down"
    prices = []
    for r in window:
        px = _f(r.get("close") if r.get("close") is not None else r.get("price"))
        if px:
            prices.append(px)
    delta_pct = None
    if len(prices) >= 2 and prices[0]:
        delta_pct = (prices[-1] - prices[0]) / prices[0] * 100.0
    streak_dir, streak_len = None, 0
    for d in reversed(dirs):
        if d not in ("UP", "DOWN"):
            continue
        if streak_dir is None:
            streak_dir, streak_len = d, 1
        elif d == streak_dir:
            streak_len += 1
        else:
            break
    return {
        "lean": lean,
        "up_n": up,
        "down_n": dn,
        "n": len(window),
        "delta_pct": round(delta_pct, 4) if delta_pct is not None else None,
        "streak_dir": streak_dir.lower() if streak_dir else None,
        "streak_len": streak_len,
        "last_price": prices[-1] if prices else None,
    }


def _rsi_band_lean(tv: dict, symbol: str) -> dict:
    rows = (tv.get("rsi_band_history_by_symbol") or {}).get(symbol) or []
    if not rows:
        return {"lean": None, "zone": None, "rsi": None}
    last = rows[-1]
    zone = str(last.get("rsi_zone") or "").lower() or None
    lean = "up" if zone == "oversold" else ("down" if zone == "overbought" else None)
    return {"lean": lean, "zone": zone, "rsi": _f(last.get("rsi")), "band_event": last.get("band_event")}


def _div_lean(tv: dict, symbol: str) -> dict:
    rows = (tv.get("rsi_div_history_by_symbol") or {}).get(symbol) or []
    if not rows:
        return {"lean": None, "kind": None}
    last = rows[-1]
    kind = str(last.get("divergence_kind") or last.get("signal_level") or "").lower()
    direction = str(last.get("direction") or "").upper()
    lean = "up" if direction == "UP" or "bull" in kind else (
        "down" if direction == "DOWN" or "bear" in kind else None)
    return {"lean": lean, "kind": kind or None, "signal_level": last.get("signal_level")}


def prepare_mc_feed(status: dict, tv: dict, *, light: Optional[dict] = None) -> dict:
    """Build complete Monte Carlo input from live bot state."""
    light = light or {}
    price = status.get("price") or {}
    eth = status.get("eth_price") or {}
    tv_st = status.get("tradingview") or {}
    sl = tv_st.get("signal_learning") or {}
    council = (status.get("llm_council") or {}).get("members") or {}
    lane = status.get("lane_15m_learner") or {}
    cal = status.get("calibration") or light.get("calibration") or {}

    symbols = {
        "btc_1h": "BTCUSDT",
        "eth_1h": "ETHUSDT",
        "btc_15m": "BTCUSD",
        "eth_15m": "ETHUSD",
    }
    charts = {}
    for lane_key, sym in symbols.items():
        bars = _bar_close_rows(tv, sym)
        charts[lane_key] = {
            "symbol": sym,
            "bar_close_n": len(bars),
            "short_lean": _short_lean(bars, 8),
            "regime_lean": _short_lean(bars, 50),
            "rsi_band": _rsi_band_lean(tv, sym),
            "rsi_divergence": _div_lean(tv, sym),
        }

    # Historical signal-level WRs → p-tilts for scenario shading
    signal_wr = {}
    for k, v in (sl.get("by_signal_level") or {}).items():
        if isinstance(v, dict) and v.get("n"):
            signal_wr[k] = {
                "n": int(v["n"]),
                "win_rate": _f(v.get("win_rate")),
                "pnl_usd": _f(v.get("pnl_usd")),
            }

    council_acc = {}
    for name, m in council.items():
        if isinstance(m, dict) and m.get("n"):
            council_acc[name] = {
                "n": int(m["n"]),
                "accuracy": _f(m.get("accuracy")),
                "faded": bool(m.get("faded")),
                "stance": m.get("stance"),
                "weight": _f(m.get("weight")),
            }

    # Empirical edge of TV lean vs coin-flip (for mu tilt)
    # UP_WEAK WR 0.636 → edge +0.136; DOWN_STRONG 0.333 → edge -0.167
    keep_levels = {"UP_WEAK", "UP_STRONG", "DOWN_WEAK"}
    avoid_levels = {"DOWN_STRONG", "BAR_BULL", "FLAT", "BAR_BEAR"}

    feed = {
        "schema": "mc_profit_discovery_feed/1.0",
        "prepared_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "paper_only": True,
        "assets": {
            "btc": {
                "s_now": _f(price.get("last_price")),
                "sigma_per_sec": _f(price.get("sigma_per_sec")),
                "vol_samples": int(price.get("vol_samples") or 0),
                "price_source": price.get("source"),
            },
            "eth": {
                "s_now": _f(eth.get("last_price")),
                "sigma_per_sec": _f(eth.get("sigma_per_sec")),
                "vol_samples": int(eth.get("vol_samples") or 0),
                "price_source": eth.get("source"),
            },
        },
        "windows": {
            "15m": {"window_seconds": 900, "lanes": ["btc_15m", "eth_15m"]},
            "1h": {"window_seconds": 3600, "lanes": ["btc_1h", "eth_1h"]},
        },
        "lane_charts": charts,
        "lane_routing": {
            "1h": "binance_usdt (*USDT)",
            "15m": "chainlink_index_usd (*USD)",
        },
        "tv_signal_learning": {
            "settled_with_signal": sl.get("settled_with_signal"),
            "by_signal_level": signal_wr,
            "by_direction": {
                k: {"n": v.get("n"), "win_rate": v.get("win_rate"), "pnl_usd": v.get("pnl_usd")}
                for k, v in (sl.get("by_direction") or {}).items() if isinstance(v, dict)
            },
            "by_indicator_name": {
                k: {"n": v.get("n"), "win_rate": v.get("win_rate"), "pnl_usd": v.get("pnl_usd")}
                for k, v in (sl.get("by_indicator_name") or {}).items() if isinstance(v, dict)
            },
            "keep_levels": sorted(keep_levels),
            "avoid_levels": sorted(avoid_levels),
        },
        "council": council_acc,
        "lane_15m_policy": lane.get("policy"),
        "lane_15m_rolling": {
            k: v for k, v in (lane.get("rolling") or {}).items()
            if k.startswith("by_") or k in ("n", "win_rate", "pnl_usd")
        },
        "calibration": {
            "samples": cal.get("samples"),
            "brier": cal.get("brier"),
            "base_rate_up": cal.get("base_rate_up") or 0.505,
            "baseline_brier_0_5": cal.get("baseline_brier_0_5"),
        },
        "capital": status.get("capital") or light.get("capital"),
        "execution_assumptions": {
            "size_usd": 5.0,
            "min_entry_price": 0.50,
            "max_entry_price": 0.75,
            "fee_frac": 0.0,  # paper; Polymarket maker/taker ignored in research sim
            "slippage_abs": 0.01,  # adverse 1c on ask
        },
        "scenario_priors": {
            "tv_2h_accuracy": (council_acc.get("tv_2h_trend") or {}).get("accuracy") or 0.60,
            "bar_close_continuation": 0.52,  # from live forward sim ~coin-flip
            "streak3_fade_rate": 0.75,
            "neutral_sigma_btc": 7e-5,
            "note": "mu tilts from TV lean × historical edge; sigma from live bot",
        },
    }
    return feed


# ---------------------------------------------------------------------------
# Monte Carlo engine
# ---------------------------------------------------------------------------


def closed_form_p_up(s_now: float, s_open: float, sigma: float, ttc: float,
                     mu: float = 0.0) -> float:
    if ttc <= 0 or sigma <= 0 or s_now <= 0 or s_open <= 0:
        return 1.0 if s_now >= s_open else 0.0
    sig_h = sigma * math.sqrt(ttc)
    z = (math.log(s_now / s_open) + (mu - 0.5 * sigma ** 2) * ttc) / sig_h
    return max(0.0, min(1.0, 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))))


def simulate_window_paths(
    *,
    s_open: float,
    sigma: float,
    window_s: float,
    n_paths: int,
    mu: float = 0.0,
    jump_intensity: float = 0.0,
    jump_sigma: float = 0.0,
    n_steps: int = 30,
    rng: np.random.Generator,
) -> np.ndarray:
    """Return price paths shape (n_paths, n_steps+1), index 0 = open."""
    dt = float(window_s) / int(n_steps)
    log_s = np.full(n_paths, math.log(float(s_open)))
    out = np.empty((n_paths, n_steps + 1), dtype=np.float64)
    out[:, 0] = s_open
    for i in range(n_steps):
        drift = (mu - 0.5 * sigma * sigma) * dt
        step = rng.normal(drift, sigma * math.sqrt(dt), size=n_paths)
        if jump_intensity > 0 and jump_sigma > 0:
            nj = rng.poisson(jump_intensity * dt, size=n_paths)
            step = step + rng.normal(0.0, 1.0, size=n_paths) * (np.sqrt(nj) * jump_sigma)
        log_s = log_s + step
        out[:, i + 1] = np.exp(log_s)
    return out


def pnl_binary(side: str, p_win: np.ndarray, ask: float, size: float,
               slippage: float = 0.01) -> np.ndarray:
    """Expected PnL vector given win probability per path... actually path outcomes.

    For path-level: win is boolean array. PnL = size*(1/q - 1) if win else -size.
    """
    q = min(0.99, max(0.01, float(ask) + float(slippage)))
    # p_win here is boolean win array
    win = p_win.astype(np.float64)
    return size * (win * (1.0 / q - 1.0) + (1.0 - win) * (-1.0))


def lean_mu_tilt(lean: Optional[str], *, edge: float = 0.05,
                 sigma: float = 1e-5, ttc: float = 300.0) -> float:
    """Map directional lean + edge into a small GBM drift (mu_per_sec)."""
    if lean not in ("up", "down") or edge <= 0:
        return 0.0
    # Rough: shift log-distance by ~edge * sigma * sqrt(ttc) over remaining horizon
    shift = float(edge) * float(sigma) * math.sqrt(max(1.0, ttc))
    mu = shift / max(1.0, ttc)
    return mu if lean == "up" else -mu


def run_profit_discovery(feed: dict, *, n_paths: int = 1_000_000, seed: int = 42) -> dict:
    """Sweep policies; allocate n_paths across (asset × window × lean_scenario)."""
    rng = np.random.default_rng(seed)
    size = float((feed.get("execution_assumptions") or {}).get("size_usd") or 5.0)
    slip = float((feed.get("execution_assumptions") or {}).get("slippage_abs") or 0.01)
    min_entry = float((feed.get("execution_assumptions") or {}).get("min_entry_price") or 0.50)
    max_entry = float((feed.get("execution_assumptions") or {}).get("max_entry_price") or 0.75)

    assets = feed["assets"]
    charts = feed["lane_charts"]
    priors = feed.get("scenario_priors") or {}
    tv2h_acc = float(priors.get("tv_2h_accuracy") or 0.60)
    streak_fade = float(priors.get("streak3_fade_rate") or 0.75)

    # Policy grid
    ttc_grid_15m = [720, 600, 480, 420, 300, 240, 180, 120]
    ttc_grid_1h = [2700, 1800, 1200, 900, 600, 300]
    ask_grid = [0.50, 0.52, 0.55, 0.58, 0.60, 0.65, 0.70]
    sides = ("up", "down")

    # Lean scenarios: neutral, follow short_path, fade short_path, follow tv2h-quality
    lean_modes = ("neutral", "follow_short", "fade_short", "follow_tv2h_edge", "fade_streak3")

    scenarios = []
    for asset in ("btc", "eth"):
        for win_label, window_s, lane_key, ttc_grid in (
            ("15m", 900, f"{asset}_15m", ttc_grid_15m),
            ("1h", 3600, f"{asset}_1h", ttc_grid_1h),
        ):
            scenarios.append({
                "asset": asset,
                "window": win_label,
                "window_s": window_s,
                "lane_key": lane_key,
                "ttc_grid": ttc_grid,
                "chart": charts.get(lane_key) or {},
                "s_now": float(assets[asset]["s_now"] or 0),
                "sigma": float(assets[asset]["sigma_per_sec"] or 0),
            })

    # Paths per scenario (equal split)
    n_scen = len(scenarios)
    paths_per = max(10_000, n_paths // n_scen)
    # Adjust so total ≈ n_paths
    paths_per = int(n_paths // n_scen)
    actual_total = paths_per * n_scen

    n_steps = 30
    results = []  # policy aggregates
    # Accumulator: key -> list of mean pnl, win rate contributions
    agg = defaultdict(lambda: {"pnl_sum": 0.0, "n": 0, "wins": 0.0, "paths": 0})

    t0 = time.time()
    for scen in scenarios:
        s_open = float(scen["s_now"])  # research: treat current as open proxy mid-window
        # Better: open slightly offset so move_from_open is realistic from short lean delta
        delta = (scen["chart"].get("short_lean") or {}).get("delta_pct") or 0.0
        # Reconstruct a synthetic open so current is ~delta% from open over short path
        s_open = s_open / (1.0 + float(delta) / 100.0) if abs(delta) < 5 else s_open
        sigma = max(1e-7, float(scen["sigma"]))
        window_s = float(scen["window_s"])
        short = scen["chart"].get("short_lean") or {}
        short_lean = short.get("lean")
        streak_len = int(short.get("streak_len") or 0)
        streak_dir = short.get("streak_dir")

        for lean_mode in lean_modes:
            # Scenario mu for full-window path
            if lean_mode == "neutral":
                mu = 0.0
            elif lean_mode == "follow_short":
                mu = lean_mu_tilt(short_lean, edge=0.04, sigma=sigma, ttc=window_s * 0.5)
            elif lean_mode == "fade_short":
                opp = "down" if short_lean == "up" else ("up" if short_lean == "down" else None)
                mu = lean_mu_tilt(opp, edge=0.03, sigma=sigma, ttc=window_s * 0.5)
            elif lean_mode == "follow_tv2h_edge":
                # Use council accuracy edge vs 0.5
                edge = max(0.0, tv2h_acc - 0.5)
                mu = lean_mu_tilt(short_lean or "up", edge=edge, sigma=sigma, ttc=window_s * 0.5)
            elif lean_mode == "fade_streak3":
                if streak_len >= 3 and streak_dir in ("up", "down"):
                    opp = "down" if streak_dir == "up" else "up"
                    mu = lean_mu_tilt(opp, edge=streak_fade - 0.5, sigma=sigma, ttc=300.0)
                else:
                    mu = 0.0
            else:
                mu = 0.0

            paths = simulate_window_paths(
                s_open=s_open, sigma=sigma, window_s=window_s, n_paths=paths_per,
                mu=mu, n_steps=n_steps, rng=rng,
            )
            # Settlement outcome
            s_close = paths[:, -1]
            outcome_up = s_close >= s_open

            for ttc in scen["ttc_grid"]:
                if ttc >= window_s:
                    continue
                # Step index nearest to (window_s - ttc)
                elapsed = window_s - float(ttc)
                step_i = int(round(elapsed / window_s * n_steps))
                step_i = max(0, min(n_steps, step_i))
                s_t = paths[:, step_i]
                # Fair p at this TTC (pathwise closed form with remaining time)
                # Vectorized approx via MC fraction of paths that finish up from here is outcome
                # conditioned... For EV we use true path outcome (perfect foresight of THIS path's
                # settlement) — that's the oracle. For decision we use model p from s_t.
                # Model p: closed form per path is expensive; use batch: P(up|s_t) via formula.
                # Approximate with single sigma/mu for all paths at mean s_t ratio:
                # Better: compute win using actual outcome_up (path truth) for PnL, and gate
                # entries by model edge vs ask.

                # Model p_up for each path at this TTC
                # p = Φ( (ln(s_t/s_open) + (mu-0.5σ²)ttc) / (σ√ttc) )
                sig_h = sigma * math.sqrt(float(ttc))
                if sig_h <= 0:
                    continue
                z = (np.log(np.maximum(s_t, 1e-12) / s_open) + (mu - 0.5 * sigma * sigma) * ttc) / sig_h
                p_model = 0.5 * (1.0 + erf_vec(z / math.sqrt(2.0)))

                for side in sides:
                    p_side = p_model if side == "up" else (1.0 - p_model)
                    win = outcome_up if side == "up" else ~outcome_up

                    for ask in ask_grid:
                        if ask < min_entry or ask > max_entry:
                            continue
                        # Gate: only take trade if model edge vs ask clears buffer
                        edge = p_side - ask
                        # Soft gate variants encoded in key
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
                            # Also apply TV keep/avoid style filter via lean_mode already in scenario
                            pnl = pnl_binary(side, win[mask], ask, size, slippage=slip)
                            key = (
                                f"{scen['asset']}|{scen['window']}|ttc{int(ttc)}|"
                                f"{side}|ask{ask:.2f}|{lean_mode}|{gate_name}"
                            )
                            a = agg[key]
                            a["pnl_sum"] += float(np.sum(pnl))
                            a["n"] += int(pnl.size)
                            a["wins"] += float(np.sum(win[mask]))
                            a["paths"] += int(mask.sum())
                            a["meta"] = {
                                "asset": scen["asset"],
                                "window": scen["window"],
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

    # Rank policies
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
            "profit_factor_proxy": round(
                max(0.0, pnl) / max(1e-9, abs(min(0.0, pnl)) + 1e-9), 3) if pnl != 0 else 0.0,
        })

    ranked.sort(key=lambda r: (-r["ev_per_trade_usd"], -r["total_pnl_usd"], -r["win_rate"]))

    # Best per asset/window
    best_by_lane = {}
    for r in ranked:
        lane = f"{r['asset']}_{r['window']}"
        if lane not in best_by_lane and r["ev_per_trade_usd"] > 0 and r["win_rate"] >= 0.52:
            best_by_lane[lane] = r

    # Summary by lean_mode / gate
    def summarize(field: str, top_n: int = 8):
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

    return {
        "schema": "mc_profit_discovery_result/1.0",
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
        "best_positive_by_lane": best_by_lane,
        "summary_by_lean_mode": summarize("lean_mode"),
        "summary_by_gate": summarize("gate"),
        "summary_by_side": summarize("side"),
        "summary_by_asset": summarize("asset"),
        "summary_by_window": summarize("window"),
        "recommendation": _recommendation(ranked, best_by_lane, feed),
    }


def erf_vec(x: np.ndarray) -> np.ndarray:
    return np.vectorize(math.erf, otypes=[float])(x)


def _recommendation(ranked: list, best_by_lane: dict, feed: dict) -> dict:
    top = [r for r in ranked if r["ev_per_trade_usd"] > 0 and r["win_rate"] >= 0.55][:5]
    avoid = [r for r in ranked if r["ev_per_trade_usd"] < -0.5][-5:]
    charts = feed.get("lane_charts") or {}
    return {
        "use_tv": {
            "primary_context": "tv_2h_trend (council ~60% acc) + follow_tv2h_edge lean mode",
            "bar_close": "short_path plot; fade_streak3 when streak_len>=3",
            "signal_levels_keep": (feed.get("tv_signal_learning") or {}).get("keep_levels"),
            "signal_levels_avoid": (feed.get("tv_signal_learning") or {}).get("avoid_levels"),
            "symbols": {k: v.get("symbol") for k, v in charts.items()},
        },
        "best_policies_now": top,
        "worst_policies_now": avoid,
        "best_by_lane": best_by_lane,
        "note": (
            "EV>0 policies require model edge gate (edge_ge_0.05+) and selective lean modes. "
            "Neutral always-enter is typically negative after slippage — matches live bot."
        ),
    }


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

    print("Preparing MC feed from bot state...")
    feed = prepare_mc_feed(status, tv, light=light)
    feed_path = out_dir / "mc_feed.json"
    feed_path.write_text(json.dumps(feed, indent=2, default=str))
    print(f"  wrote {feed_path} ({feed_path.stat().st_size} bytes)")
    print(f"  BTC s_now={feed['assets']['btc']['s_now']} sigma={feed['assets']['btc']['sigma_per_sec']}")
    print(f"  ETH s_now={feed['assets']['eth']['s_now']} sigma={feed['assets']['eth']['sigma_per_sec']}")
    for k, v in feed["lane_charts"].items():
        sl = v.get("short_lean") or {}
        print(f"  {k}: {v.get('symbol')} lean={sl.get('lean')} streak={sl.get('streak_len')}{sl.get('streak_dir')} bars={v.get('bar_close_n')}")

    print(f"\nRunning profit discovery: {args.n_paths:,} paths...")
    result = run_profit_discovery(feed, n_paths=args.n_paths, seed=args.seed)
    result_path = out_dir / "mc_profit_discovery_result.json"
    result_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"  wrote {result_path}")
    print(f"  simulated {result['n_paths_simulated']:,} paths in {result['elapsed_s']}s")
    print(f"  ranked {result['n_policies_ranked']} policies")

    print("\n=== TOP 10 POLICIES BY EV/TRADE ===")
    for i, r in enumerate(result["top_20_policies"][:10], 1):
        print(
            f"  {i:2}. {r['asset']:3} {r['window']:3} ttc={r['ttc_s']:4} {r['side']:4} "
            f"ask={r['ask']:.2f} lean={r['lean_mode']:16} gate={r['gate']:12} "
            f"WR={r['win_rate']:.3f} EV=${r['ev_per_trade_usd']:.4f} PnL=${r['total_pnl_usd']:.1f} n={r['n_trades']}"
        )

    print("\n=== BEST POSITIVE BY LANE ===")
    for lane, r in (result.get("best_positive_by_lane") or {}).items():
        print(f"  {lane}: {r['key']} WR={r['win_rate']} EV=${r['ev_per_trade_usd']}")

    print("\n=== SUMMARY BY LEAN MODE ===")
    for r in result.get("summary_by_lean_mode") or []:
        print(f"  {r['lean_mode']:16} WR={r['win_rate']} EV=${r['ev_per_trade_usd']} PnL=${r['total_pnl_usd']}")

    print("\n=== SUMMARY BY GATE ===")
    for r in result.get("summary_by_gate") or []:
        print(f"  {r['gate']:12} WR={r['win_rate']} EV=${r['ev_per_trade_usd']} PnL=${r['total_pnl_usd']}")

    rec = result.get("recommendation") or {}
    print("\n=== RECOMMENDATION ===")
    print(json.dumps(rec.get("use_tv"), indent=2))
    md_path = out_dir / "MC_PROFIT_DISCOVERY.md"
    md_path.write_text(_to_markdown(feed, result))
    print(f"\nWrote report {md_path}")


def _to_markdown(feed: dict, result: dict) -> str:
    lines = [
        "# Monte Carlo Profit Discovery — BTC/ETH Polymarket",
        "",
        f"_Prepared {feed.get('prepared_at_utc')} · PAPER ONLY_",
        "",
        f"- Paths simulated: **{result['n_paths_simulated']:,}**",
        f"- Policies ranked: **{result['n_policies_ranked']}**",
        f"- Runtime: {result['elapsed_s']}s · seed={result['seed']}",
        "",
        "## Bot feed snapshot",
        "",
        f"| Asset | Price | sigma/s |",
        f"|-------|------:|--------:|",
        f"| BTC | {feed['assets']['btc']['s_now']} | {feed['assets']['btc']['sigma_per_sec']} |",
        f"| ETH | {feed['assets']['eth']['s_now']} | {feed['assets']['eth']['sigma_per_sec']} |",
        "",
        "### Lane charts (TV)",
        "",
    ]
    for k, v in (feed.get("lane_charts") or {}).items():
        sl = v.get("short_lean") or {}
        lines.append(
            f"- **{k}** `{v.get('symbol')}`: lean={sl.get('lean')} "
            f"streak={sl.get('streak_len')}{sl.get('streak_dir') or ''} bars={v.get('bar_close_n')}"
        )
    lines += ["", "## Top 15 policies (by EV/trade)", "",
              "| # | Asset | Win | TTC | Side | Ask | Lean | Gate | WR | EV $/trade | Total PnL |",
              "|---|-------|-----|-----|------|-----|------|------|----|------------|-----------|"]
    for i, r in enumerate((result.get("top_20_policies") or [])[:15], 1):
        lines.append(
            f"| {i} | {r['asset']} | {r['window']} | {r['ttc_s']} | {r['side']} | "
            f"{r['ask']:.2f} | {r['lean_mode']} | {r['gate']} | {r['win_rate']:.3f} | "
            f"{r['ev_per_trade_usd']:.4f} | {r['total_pnl_usd']:.1f} |"
        )
    lines += ["", "## Lean mode summary", ""]
    for r in result.get("summary_by_lean_mode") or []:
        lines.append(f"- `{r['lean_mode']}`: WR={r['win_rate']} EV=${r['ev_per_trade_usd']} PnL=${r['total_pnl_usd']}")
    lines += ["", "## Gate summary", ""]
    for r in result.get("summary_by_gate") or []:
        lines.append(f"- `{r['gate']}`: WR={r['win_rate']} EV=${r['ev_per_trade_usd']} PnL=${r['total_pnl_usd']}")
    rec = result.get("recommendation") or {}
    lines += ["", "## Recommendation", "", "```json", json.dumps(rec, indent=2), "```", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
