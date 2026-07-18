"""Real historical corpus — resolved Polymarket crypto up/down markets.

Pull side (network) lives in ``scripts/pull_gamma_corpus.py``; this module is
cache-first and fully usable offline. Layout under ``data/cache/gamma/``:

    pages/markets_page_NNNN.json   raw Gamma /markets responses (immutable)
    prices/<up_token_id>.json      raw CLOB /prices-history responses
    manifest.json                  pull metadata (when, params, counts)

Honesty rules baked in:
  * scope = the live desk's series only (``hermes.market_scope.SLUG_RE``);
  * outcomes come from the API's resolved prices mapped through outcome
    names — never inferred from anything a model produced;
  * decision points carry q = p as an EXPLICIT placeholder
    (``meta.q_source = "market_placeholder_no_model"``). Model q joins in a
    later task; nothing here fabricates one;
  * every market dropped (out of scope, unresolved, no usable prices) is
    counted in the coverage summary — no silent truncation.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional, Sequence

if TYPE_CHECKING:  # pydantic import deferred — the pull path must stay light
    from models.market import DecisionPoint

# Keep the pull path importable with only stdlib+httpx: prefer the live
# scope regex, fall back to an identical copy (a test pins them equal).
try:
    from hermes.market_scope import SLUG_RE  # needs PyYAML
except ImportError:  # pragma: no cover - exercised via subprocess test
    SLUG_RE = re.compile(r"^(btc|eth|sol)-updown-(5m|15m)-(\d+)$")

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("data/cache/gamma")
DEFAULT_FRACS = (0.3, 0.6, 0.85)
# CLOB history fidelity is >= 1 minute; allow a stale quote up to this age.
DEFAULT_MAX_STALE_SEC = 150.0

WINDOW_SEC = {"5m": 300, "15m": 900}


def _parse_json_field(raw: Any) -> list:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return list(raw) if isinstance(raw, (list, tuple)) else []


def _parse_iso(ts: Any) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class UpDownMarket:
    """One resolved (or pending) crypto up/down market from Gamma."""

    market_id: str
    slug: str
    asset: str
    timeframe: str
    window_sec: int
    open_ts: float
    close_ts: float
    outcome_up: Optional[bool]  # None until resolved
    clob_token_up: str
    clob_token_down: str
    volume: float
    liquidity: float
    question: str = ""
    condition_id: str = ""


def parse_updown_market(row: dict[str, Any]) -> Optional[UpDownMarket]:
    """Parse a Gamma /markets row; None if outside the live up/down scope."""
    slug = str(row.get("slug") or "").strip().lower()
    m = SLUG_RE.match(slug)
    if not m:
        return None
    asset, tf = m.group(1), m.group(2)
    window_sec = WINDOW_SEC.get(tf)
    open_ts = _parse_iso(row.get("startDate"))
    close_ts = _parse_iso(row.get("endDate"))
    if window_sec is None or open_ts is None or close_ts is None:
        return None

    outcomes = [str(o).strip().lower() for o in _parse_json_field(row.get("outcomes"))]
    tokens = [str(t) for t in _parse_json_field(row.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(tokens) != 2 or "up" not in outcomes:
        return None
    up_i = outcomes.index("up")
    down_i = 1 - up_i

    outcome_up: Optional[bool] = None
    if bool(row.get("closed")):
        prices = _parse_json_field(row.get("outcomePrices"))
        if len(prices) == 2:
            try:
                up_price = float(prices[up_i])
            except (TypeError, ValueError):
                up_price = None  # type: ignore[assignment]
            # Resolved binaries settle ~1/0; anything mid means not resolved yet
            if up_price is not None and (up_price >= 0.95 or up_price <= 0.05):
                outcome_up = up_price >= 0.95

    return UpDownMarket(
        market_id=str(row.get("id") or row.get("conditionId") or slug),
        slug=slug,
        asset=asset,
        timeframe=tf,
        window_sec=int(window_sec),
        open_ts=float(open_ts),
        close_ts=float(close_ts),
        outcome_up=outcome_up,
        clob_token_up=tokens[up_i],
        clob_token_down=tokens[down_i],
        volume=float(row.get("volumeNum") or row.get("volume") or 0.0),
        liquidity=float(row.get("liquidityNum") or row.get("liquidity") or 0.0),
        question=str(row.get("question") or "")[:160],
        condition_id=str(row.get("conditionId") or ""),
    )


def parse_price_history(payload: dict[str, Any]) -> list[tuple[float, float]]:
    """CLOB /prices-history → sorted [(epoch_sec, up_price), ...]."""
    out: list[tuple[float, float]] = []
    for pt in payload.get("history") or []:
        try:
            t = float(pt["t"])
            p = float(pt["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0.0 < p < 1.0:
            out.append((t, p))
    return sorted(out)


def reconstruct_decisions(
    market: UpDownMarket,
    history: Sequence[tuple[float, float]],
    *,
    fracs: Sequence[float] = DEFAULT_FRACS,
    max_stale_sec: float = DEFAULT_MAX_STALE_SEC,
    t0_epoch: Optional[float] = None,
) -> list[DecisionPoint]:
    """Rebuild no-lookahead decision points from cached CLOB history.

    For each window fraction, p is the last traded UP price at or before the
    decision timestamp; decisions with no quote fresher than
    ``max_stale_sec`` are dropped (the caller counts them).
    """
    from models.market import DecisionPoint  # deferred: pull path stays light

    if market.outcome_up is None or not history:
        return []
    t0 = float(t0_epoch if t0_epoch is not None else market.open_ts)
    out: list[DecisionPoint] = []
    for frac in fracs:
        t_d = market.open_ts + float(frac) * market.window_sec
        if t_d >= market.close_ts:
            continue
        quote: Optional[tuple[float, float]] = None
        for t, p in history:
            if t <= t_d:
                quote = (t, p)
            else:
                break
        if quote is None or (t_d - quote[0]) > max_stale_sec:
            continue
        seconds_left = market.close_ts - t_d
        out.append(
            DecisionPoint(
                market_id=market.market_id,
                decision_id=f"{market.slug}_f{int(round(frac * 100)):02d}",
                decision_time=(t_d - t0) / 86400.0,
                lifetime_frac=float(frac),
                category="crypto",
                days_to_resolution=seconds_left / 86400.0,
                p=float(quote[1]),
                # EXPLICIT placeholder — no model has produced a q for this
                # timestamp yet. Filled by the CEX-join task; never fabricated.
                q=float(quote[1]),
                liquidity_usd=market.liquidity,
                volume_24h=market.volume,
                true_q=1.0 if market.outcome_up else 0.0,  # realized outcome (diagnostic)
                resolved_yes=bool(market.outcome_up),
                resolution_time=(market.close_ts - t0) / 86400.0,
                meta={
                    "source": "gamma_corpus",
                    "q_source": "market_placeholder_no_model",
                    "asset": market.asset,
                    "timeframe": market.timeframe,
                    "slug": market.slug,
                    "quote_ts": quote[0],
                    "quote_age_sec": t_d - quote[0],
                    "decision_epoch": t_d,
                },
            )
        )
    return out


@dataclass
class CorpusSummary:
    """Coverage accounting — every drop is counted, nothing silent."""

    n_rows_seen: int = 0
    n_in_scope: int = 0
    n_resolved: int = 0
    n_with_prices: int = 0
    n_decisions: int = 0
    first_close_iso: str = ""
    last_close_iso: str = ""
    by_series: dict[str, int] = field(default_factory=dict)

    def text(self) -> str:
        return (
            f"coverage: rows_seen={self.n_rows_seen} in_scope={self.n_in_scope} "
            f"resolved={self.n_resolved} with_usable_prices={self.n_with_prices} "
            f"decisions={self.n_decisions} span=[{self.first_close_iso} .. "
            f"{self.last_close_iso}] by_series={self.by_series}"
        )


@dataclass
class Corpus:
    markets: list[UpDownMarket]
    decisions: list[DecisionPoint]
    summary: CorpusSummary


def iter_cached_rows(cache_dir: Path) -> Iterable[dict[str, Any]]:
    pages = sorted((cache_dir / "pages").glob("markets_page_*.json"))
    seen: set[str] = set()
    for page in pages:
        try:
            rows = json.loads(page.read_text())
        except json.JSONDecodeError:
            logger.warning("gamma_corpus: unreadable page %s", page)
            continue
        for row in rows if isinstance(rows, list) else []:
            rid = str(row.get("id") or row.get("slug") or "")
            if rid and rid in seen:
                continue
            seen.add(rid)
            yield row


def load_corpus(
    *,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    fracs: Sequence[float] = DEFAULT_FRACS,
    max_stale_sec: float = DEFAULT_MAX_STALE_SEC,
) -> Corpus:
    """Build the decision corpus from the local cache (no network)."""
    cache = Path(cache_dir)
    s = CorpusSummary()
    markets: list[UpDownMarket] = []
    covered: list[tuple[UpDownMarket, list[tuple[float, float]]]] = []

    for row in iter_cached_rows(cache):
        s.n_rows_seen += 1
        m = parse_updown_market(row)
        if m is None:
            continue
        s.n_in_scope += 1
        markets.append(m)
        if m.outcome_up is None:
            continue
        s.n_resolved += 1
        price_file = cache / "prices" / f"{m.clob_token_up}.json"
        if not price_file.is_file():
            continue
        try:
            hist = parse_price_history(json.loads(price_file.read_text()))
        except json.JSONDecodeError:
            logger.warning("gamma_corpus: unreadable prices %s", price_file)
            continue
        if not hist:
            continue
        s.n_with_prices += 1
        covered.append((m, hist))

    decisions: list[DecisionPoint] = []
    if covered:
        t0 = min(m.open_ts for m, _ in covered)
        closes = sorted(m.close_ts for m, _ in covered)
        s.first_close_iso = datetime.fromtimestamp(closes[0], tz=timezone.utc).isoformat()
        s.last_close_iso = datetime.fromtimestamp(closes[-1], tz=timezone.utc).isoformat()
        for m, hist in covered:
            ds = reconstruct_decisions(
                m, hist, fracs=fracs, max_stale_sec=max_stale_sec, t0_epoch=t0
            )
            decisions.extend(ds)
            key = f"{m.asset}_{m.timeframe}"
            s.by_series[key] = s.by_series.get(key, 0) + 1
    decisions.sort(key=lambda d: (d.decision_time, d.decision_id))
    s.n_decisions = len(decisions)
    logger.info("gamma_corpus: %s", s.text())
    return Corpus(markets=markets, decisions=decisions, summary=s)


def sample_report(
    *,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    n: int = 20,
) -> str:
    """Human-readable sample of the corpus for approval before engine wiring."""
    corpus = load_corpus(cache_dir=cache_dir)
    lines = [
        "=== Gamma corpus sample (real resolved crypto up/down markets) ===",
        corpus.summary.text(),
        "",
        f"{'slug':40s} {'close (UTC)':22s} {'outcome':8s} {'vol$':>10s} {'decisions':>9s}",
    ]
    resolved = [m for m in corpus.markets if m.outcome_up is not None]
    dec_by_market: dict[str, int] = {}
    for d in corpus.decisions:
        dec_by_market[d.market_id] = dec_by_market.get(d.market_id, 0) + 1
    for m in resolved[: max(1, n)]:
        close_iso = datetime.fromtimestamp(m.close_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        lines.append(
            f"{m.slug:40s} {close_iso:22s} {'UP' if m.outcome_up else 'DOWN':8s} "
            f"{m.volume:>10.0f} {dec_by_market.get(m.market_id, 0):>9d}"
        )
    dp = [d for d in corpus.decisions[:6]]
    if dp:
        lines += ["", "first decision points (t, slug, p, secs_left, outcome):"]
        for d in dp:
            lines.append(
                f"  {d.meta['decision_epoch']:.0f} {d.meta['slug']:38s} "
                f"p={d.p:.3f} left={d.days_to_resolution * 86400:.0f}s "
                f"{'UP' if d.resolved_yes else 'DOWN'}"
            )
    return "\n".join(lines)
