"""Offline historical replay from Polymarket backfill (PAPER ONLY).

Builds enriched settled positions from Gamma winners + CLOB price paths, then
feeds lane_15m_learner, directional_cell_learning, and CHRONOS walk-forward.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("pulse.offline_replay")

SERIES_SPECS = (
    {"asset": "btc", "lane": "15m", "series_slug": "btc-up-or-down-15m", "window_seconds": 900},
    {"asset": "eth", "lane": "15m", "series_slug": "eth-up-or-down-15m", "window_seconds": 900},
    {"asset": "btc", "lane": "1h", "series_slug": "btc-up-or-down-hourly", "window_seconds": 3600},
    {"asset": "eth", "lane": "1h", "series_slug": "eth-up-or-down-hourly", "window_seconds": 3600},
)

ENTRY_FRACS = {
    "early": 0.20,
    "mid": 0.50,
    "late": 0.70,
}


@dataclass
class EnrichedWindow:
    event_slug: str
    event_id: str
    market_id: str
    condition_id: str
    asset: str
    lane: str
    series_slug: str
    window_seconds: int
    open_ts: float
    close_ts: float
    up_token_id: str
    down_token_id: str
    winner: str
    up_won: bool


def _parse_json_field(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
    return []


def _iso_to_unix(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def winner_from_market(m: dict) -> tuple[str, Optional[bool]]:
    outs = _parse_json_field(m.get("outcomes"))
    prices = _parse_json_field(m.get("outcomePrices"))
    if not outs or not prices or len(outs) != len(prices):
        return "unknown", None
    try:
        floats = [float(p) for p in prices]
    except (TypeError, ValueError):
        return "unknown", None
    if max(floats) < 0.5:
        return "unknown", None
    idx = floats.index(max(floats))
    name = str(outs[idx]).strip().lower()
    if name in ("up", "yes"):
        return "up", True
    if name in ("down", "no"):
        return "down", False
    return "unknown", None


def parse_window_event(ev: dict, spec: dict) -> Optional[EnrichedWindow]:
    markets = ev.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    toks = _parse_json_field(m.get("clobTokenIds"))
    outs = _parse_json_field(m.get("outcomes"))
    if len(toks) < 2:
        return None
    winner, up_won = winner_from_market(m)
    if up_won is None:
        return None

    close_ts = _iso_to_unix(m.get("endDate") or ev.get("endDate"))
    if close_ts is None:
        return None
    open_ts = _iso_to_unix(m.get("startDate") or ev.get("startDate"))
    ws = int(spec["window_seconds"])
    if open_ts is None or (close_ts - open_ts) > ws * 3:
        open_ts = close_ts - ws
    if spec["lane"] == "15m":
        open_ts = close_ts - 900.0

    up_tok = down_tok = None
    for name, tok in zip(outs, toks):
        n = str(name).strip().lower()
        if n in ("up", "yes"):
            up_tok = str(tok)
        elif n in ("down", "no"):
            down_tok = str(tok)
    if up_tok is None or down_tok is None:
        up_tok, down_tok = str(toks[0]), str(toks[1])

    return EnrichedWindow(
        event_slug=str(ev.get("slug") or m.get("slug") or ""),
        event_id=str(ev.get("id") or ""),
        market_id=str(m.get("id") or ""),
        condition_id=str(m.get("conditionId") or ""),
        asset=spec["asset"],
        lane=spec["lane"],
        series_slug=spec["series_slug"],
        window_seconds=ws,
        open_ts=float(open_ts),
        close_ts=float(close_ts),
        up_token_id=up_tok,
        down_token_id=down_tok,
        winner=winner,
        up_won=bool(up_won),
    )


def load_price_history(path: Path) -> list[tuple[float, float]]:
    if not path.exists():
        return []
    try:
        hist = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for pt in hist or []:
        try:
            out.append((float(pt["t"]), float(pt["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def price_at(hist: list[tuple[float, float]], ts: float) -> Optional[float]:
    if not hist:
        return None
    best = None
    best_dt = None
    for t, p in hist:
        dt = abs(t - ts)
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best = p
    return best


def binary_pnl(won: bool, ask: float, size_usd: float = 5.0) -> float:
    if ask <= 0 or ask >= 1:
        return 0.0
    return float(size_usd) * ((1.0 / ask) - 1.0) if won else -float(size_usd)


def ask_band(ask: float) -> str:
    if 0.47 <= ask <= 0.55:
        return "sweet"
    if 0.30 <= ask < 0.47 or 0.55 < ask <= 0.70:
        return "mid"
    if ask < 0.30:
        return "tail_low"
    return "tail_high"


def iter_raw_windows(data_root: Path) -> Iterable[EnrichedWindow]:
    gamma_root = data_root / "raw" / "gamma"
    if not gamma_root.exists():
        return
    spec_by = {s["series_slug"]: s for s in SERIES_SPECS}
    for series_dir in sorted(gamma_root.iterdir()):
        if not series_dir.is_dir():
            continue
        spec = spec_by.get(series_dir.name)
        if not spec:
            continue
        for path in sorted(series_dir.glob("*.json")):
            try:
                ev = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            w = parse_window_event(ev, spec)
            if w is not None:
                yield w


def build_positions(
    data_root: Path,
    *,
    entry_modes: tuple[str, ...] = ("mid",),
    size_usd: float = 5.0,
    both_sides: bool = True,
    min_ask: float = 0.05,
    max_ask: float = 0.95,
) -> list[dict]:
    """Build settled position dicts with real asks from CLOB price history."""
    from engine.pulse.directional_cell_learning import (
        ask_band_from_price,
        minute_band_from_seconds,
    )

    prices_dir = data_root / "raw" / "clob" / "prices"
    positions: list[dict] = []
    skipped = 0

    for w in iter_raw_windows(data_root):
        up_hist = load_price_history(prices_dir / ("%s.json" % w.up_token_id))
        down_hist = load_price_history(prices_dir / ("%s.json" % w.down_token_id))
        sides = ("up", "down") if both_sides else (("up",) if w.up_won else ("down",))

        for mode in entry_modes:
            frac = ENTRY_FRACS.get(mode, 0.50)
            entry_ts = w.open_ts + frac * float(w.window_seconds)
            ttc_s = max(0.0, w.close_ts - entry_ts)
            sso = max(0.0, entry_ts - w.open_ts)

            for side in sides:
                hist = up_hist if side == "up" else down_hist
                ask = price_at(hist, entry_ts)
                if ask is None or ask < min_ask or ask > max_ask:
                    skipped += 1
                    continue
                won = (side == "up" and w.up_won) or (side == "down" and not w.up_won)
                pnl = binary_pnl(won, ask, size_usd)
                # CellKey.as_str: asset|horizon|side|minute_band|regime|tv_pattern|ask_band
                cell_key = "|".join([
                    w.asset, w.lane, side,
                    minute_band_from_seconds(sso),
                    "unknown",
                    "∅",
                    ask_band_from_price(ask),
                ])
                positions.append({
                    "status": "settled",
                    "side": side,
                    "entry_price": round(float(ask), 4),
                    "won": bool(won),
                    "pnl_usd": round(float(pnl), 4),
                    "entry_ts": float(entry_ts),
                    "opened_at": float(entry_ts),
                    "window_key": "%s|%s|%s" % (w.event_slug, side, mode),
                    "research": {
                        "series_slug": w.series_slug,
                        "window_seconds": w.window_seconds,
                        "entry_ttc_s": ttc_s,
                        "seconds_since_open_at_entry": sso,
                        "asset": w.asset,
                        "lane": w.lane,
                        "event_slug": w.event_slug,
                        "entry_mode": mode,
                        "source": "polymarket_offline_replay",
                        "winner": w.winner,
                        "ask_band": ask_band(ask),
                        "cell_learning_key": cell_key,
                        "cell_learning_tier": "probe",
                        "cell_learning_side": side,
                        "cell_learning_edge": 0.0,
                        "cell_learning_p_up": float(ask if side == "up" else (1.0 - ask)),
                        "ttc_bucket": (
                            "early" if ttc_s / w.window_seconds > 0.65
                            else ("late" if ttc_s / w.window_seconds < 0.35 else "mid")
                        ),
                    },
                })

    log.info("built %d positions (skipped %d missing/out-of-band asks)", len(positions), skipped)
    positions.sort(key=lambda p: float(p["entry_ts"]))
    return positions


def walk_forward_split(positions: list[dict], holdout_fraction: float = 0.30) -> tuple[list, list]:
    if not positions:
        return [], []
    rows = sorted(positions, key=lambda p: float(p["entry_ts"]))
    cut = max(1, int(len(rows) * (1.0 - holdout_fraction)))
    return rows[:cut], rows[cut:]


def summarize_cohort(positions: list[dict]) -> dict:
    n = len(positions)
    if n == 0:
        return {"n": 0, "wins": 0, "wr": 0.0, "pnl": 0.0, "avg_ask": None}
    wins = sum(1 for p in positions if p.get("won"))
    pnl = sum(float(p.get("pnl_usd") or 0) for p in positions)
    asks = [float(p["entry_price"]) for p in positions if p.get("entry_price") is not None]
    return {
        "n": n,
        "wins": wins,
        "wr": round(wins / n, 4),
        "pnl": round(pnl, 2),
        "avg_ask": round(sum(asks) / len(asks), 4) if asks else None,
        "breakeven_gap": round((wins / n) - (sum(asks) / len(asks)), 4) if asks else None,
    }


def report_by_dims(positions: list[dict]) -> dict:
    """WR/PnL sliced by asset × lane × ask_band × side."""
    buckets: dict[str, list] = defaultdict(list)
    for p in positions:
        rt = p.get("research") or {}
        key = "%s|%s|%s|%s" % (
            rt.get("asset") or "?",
            rt.get("lane") or "?",
            rt.get("ask_band") or ask_band(float(p.get("entry_price") or 0.5)),
            p.get("side") or "?",
        )
        buckets[key].append(p)
    out = {}
    for k, rows in sorted(buckets.items()):
        out[k] = summarize_cohort(rows)
    return out


def favorite_filter(positions: list[dict], min_ask: float = 0.48) -> list[dict]:
    """Keep only favorite-side buys (ask >= min_ask) — High-WR book."""
    return [p for p in positions if float(p.get("entry_price") or 0) >= min_ask]


def train_learners(train_rows: list[dict], *, data_dir: Optional[Path] = None) -> dict:
    """Feed train split into lane_15m + cell learning; return serializable state."""
    from engine.pulse.chronos_validator import ChronosConfig, ChronosValidator
    from engine.pulse.directional_cell_learning import DirectionalCellLearningStore
    from engine.pulse.lane_15m_learner import Lane15mStrategyLearner

    lane = Lane15mStrategyLearner()
    cells = DirectionalCellLearningStore(data_dir=data_dir, min_samples=8)
    chronos = ChronosValidator(ChronosConfig(enabled=True, min_cohort_n=4, exploration_rate=0.0))

    for p in train_rows:
        rt = p.get("research") or {}
        ws = int(rt.get("window_seconds") or 900)
        # 15m lane learner only for 15m windows
        if 600 <= ws <= 1200:
            lane.record_settled(
                won=bool(p["won"]),
                pnl_usd=float(p.get("pnl_usd") or 0),
                side=str(p.get("side") or ""),
                entry_price=float(p["entry_price"]) if p.get("entry_price") is not None else None,
                asset=str(rt.get("asset") or "btc"),
                sso=rt.get("seconds_since_open_at_entry"),
                ttc_s=rt.get("entry_ttc_s"),
                entry_mode=str(rt.get("entry_mode") or ""),
                now=float(p["entry_ts"]),
            )
        cells.record_settled(
            str(p.get("window_key") or p["entry_ts"]),
            won=bool(p["won"]),
            pnl_usd=float(p.get("pnl_usd") or 0),
            research=rt,
            save=False,
        )

    lane_adj = lane.maybe_adjust()
    if data_dir is not None:
        cells.save()

    # CHRONOS walk-forward block replay: block when Wilson LB < ask on prior cohort
    from engine.pulse.chronos_validator import normalize_positions, wilson_lb

    rows = normalize_positions(train_rows)

    def should_block(row, history):
        ctx = row.get("context")
        cohort = [h for h in history if h.get("context") == ctx]
        if len(cohort) < 4:
            return False
        wins = sum(1 for h in cohort if h.get("won"))
        lb = wilson_lb(wins, len(cohort))
        return lb < float(row.get("ask") or 0.5)

    chronos_rep = chronos.walk_forward_block_replay(rows, should_block=should_block)

    return {
        "lane_15m_learner": lane.to_state(),
        "lane_15m_adjustment": lane_adj,
        "cell_learning": cells.to_state(),
        "chronos_block_replay": chronos_rep,
        "chronos": chronos.to_state(),
        "train_summary": summarize_cohort(train_rows),
        "train_favorites": summarize_cohort(favorite_filter(train_rows)),
    }


def evaluate_holdout(holdout_rows: list[dict], *, min_ask_favorite: float = 0.48) -> dict:
    """Score holdout under all-trades vs favorites-only policies."""
    return {
        "all": summarize_cohort(holdout_rows),
        "favorites": summarize_cohort(favorite_filter(holdout_rows, min_ask_favorite)),
        "by_dims": report_by_dims(holdout_rows),
        "favorites_by_dims": report_by_dims(favorite_filter(holdout_rows, min_ask_favorite)),
    }


def write_enriched_ledger(path: Path, positions: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "enriched_training_ledger/1.0",
        "note": "Real asks from CLOB prices-history; labels from Gamma outcomePrices",
        "n_positions": len(positions),
        "positions": positions,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_windows_csv(path: Path, data_root: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(iter_raw_windows(data_root))
    if not rows:
        return 0
    fields = [
        "event_slug", "asset", "lane", "series_slug", "window_seconds",
        "open_ts", "close_ts", "winner", "up_won", "up_token_id", "down_token_id",
        "condition_id", "market_id",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: getattr(r, k) for k in fields})
    return len(rows)


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def run_pipeline(
    data_root: Path,
    *,
    out_dir: Optional[Path] = None,
    entry_modes: tuple[str, ...] = ("mid",),
    holdout_fraction: float = 0.30,
    size_usd: float = 5.0,
) -> dict:
    """Enrich → walk-forward train/test → write artifacts."""
    data_root = data_root.resolve()
    out_dir = (out_dir or (data_root / "replay")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    n_windows = write_windows_csv(out_dir / "windows.csv", data_root)
    positions = build_positions(
        data_root, entry_modes=entry_modes, size_usd=size_usd, both_sides=True)
    write_enriched_ledger(out_dir / "enriched_ledger.json", positions)

    train, holdout = walk_forward_split(positions, holdout_fraction=holdout_fraction)
    learner_state = train_learners(train, data_dir=out_dir)
    holdout_eval = evaluate_holdout(holdout)

    # Persist learner priors for VPS import
    (out_dir / "lane_15m_learner.json").write_text(
        json.dumps(learner_state["lane_15m_learner"], indent=2), encoding="utf-8")
    (out_dir / "directional_cell_learning.json").write_text(
        json.dumps({
            "schema": "directional_cell_learning/2.0",
            **learner_state["cell_learning"],
        }, indent=2),
        encoding="utf-8",
    )

    report = {
        "schema": "offline_replay_report/1.0",
        "data_root": str(data_root),
        "n_windows": n_windows,
        "n_positions": len(positions),
        "entry_modes": list(entry_modes),
        "holdout_fraction": holdout_fraction,
        "train": {
            "summary": learner_state["train_summary"],
            "favorites": learner_state["train_favorites"],
            "lane_adjustment": learner_state["lane_15m_adjustment"],
            "chronos_block_replay": learner_state["chronos_block_replay"],
            "by_dims": report_by_dims(train),
        },
        "holdout": holdout_eval,
        "recommendation": _recommend(holdout_eval),
    }
    write_report(out_dir / "walk_forward_report.json", report)
    log.info("wrote replay artifacts to %s", out_dir)
    return report


def _recommend(holdout_eval: dict) -> dict:
    """Propose gate knobs from holdout favorites vs all."""
    fav = holdout_eval.get("favorites") or {}
    all_ = holdout_eval.get("all") or {}
    tips = []
    if fav.get("n", 0) >= 20 and fav.get("wr", 0) >= (fav.get("avg_ask") or 0.5):
        tips.append("prefer favorites (ask>=0.48) — holdout WR above avg ask")
    if all_.get("wr", 0) < (all_.get("avg_ask") or 0.5):
        tips.append("all-trades underperforms breakeven — keep underdog floor / sweet band")
    # Best dim by WR with n>=20
    best = None
    for k, v in (holdout_eval.get("favorites_by_dims") or {}).items():
        if v.get("n", 0) < 20:
            continue
        if best is None or v["wr"] > best[1]["wr"]:
            best = (k, v)
    if best:
        tips.append("strongest holdout cell: %s wr=%.3f n=%d pnl=%.1f" % (
            best[0], best[1]["wr"], best[1]["n"], best[1]["pnl"]))
    return {
        "tips": tips,
        "suggested_env": {
            "PULSE_MIN_ENTRY_PRICE": "0.48" if fav.get("wr", 0) > all_.get("wr", 0) else "0",
            "PULSE_TRIAGE_BTC_SWEET_MIN": "0.48",
            "PULSE_TRIAGE_BTC_SWEET_MAX": "0.72",
            "note": "Apply only after live paper A/B confirms; training throughput mode overrides these",
        },
    }
