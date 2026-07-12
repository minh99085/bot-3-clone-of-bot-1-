#!/usr/bin/env python3
"""Bot-1 cloud loop cycle — SKILL_ANALYSIS-bound discovery + MEMORY.md persistence.

Loads trading thresholds from SKILL_ANALYSIS.md (disk-bound, no in-context guessing).
Pulls open positions and paper wallet state from the VPS pulse API, runs a lightweight
sweet-spot / tail discovery pass against public Polymarket data, then writes MEMORY.md
and LOGS.txt before exit so GitHub Actions can commit state back to the repo.

PAPER ONLY — never places live orders.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from skill_analysis_loader import SkillThresholds  # noqa: E402

MEMORY_PATH = REPO_ROOT / "MEMORY.md"
LOGS_PATH = REPO_ROOT / "LOGS.txt"
DEFAULT_VPS = "http://144.202.122.120"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

_LOG_LINES: list[str] = []
_STATE: dict[str, Any] = {
    "skill": {},
    "wallet": {},
    "open_trades": [],
    "discovery_candidates": [],
    "scan_errors": [],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log(msg: str) -> None:
    line = f"[{_utc_now()}] {msg}"
    print(line)
    _LOG_LINES.append(line)


def _fetch_json(url: str, *, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "bot-1-cloud-loop/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_with_429_retry(url: str, *, max_retries: int = 3, base_delay: float = 5.0) -> Any:
    """Skill §4 — HTTP 429 backoff, max 3 retries."""
    attempt = 0
    while True:
        try:
            return _fetch_json(url)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt >= max_retries:
                raise
            attempt += 1
            delay = base_delay * (2 ** (attempt - 1))
            _log(f"429 on {url} — backoff {delay:.0f}s (attempt {attempt}/{max_retries})")
            time.sleep(delay)


def pull_vps_state(base_url: str) -> tuple[dict, dict]:
    """Fetch pulse status + ledger from VPS (paper wallet + open trades)."""
    base = base_url.rstrip("/")
    status = _fetch_with_429_retry(f"{base}/api/polymarket/training/btc_pulse")
    ledger = _fetch_with_429_retry(f"{base}/api/polymarket/training/btc_pulse/ledger")
    return status, ledger


def _wallet_from_status(status: dict, ledger: dict) -> dict:
    cap = dict(status.get("capital") or {})
    ledger_stats = dict(ledger.get("stats") or {})
    return {
        "paper_only": True,
        "starting_capital_usd": cap.get("starting_capital_usd"),
        "on_hand_capital_usd": cap.get("on_hand_capital_usd"),
        "total_on_hand_usd": cap.get("total_on_hand_usd", cap.get("on_hand_capital_usd")),
        "realized_pnl_usd": cap.get("realized_pnl_usd", cap.get("total_realized_pnl_usd")),
        "total_realized_pnl_usd": cap.get("total_realized_pnl_usd"),
        "open_exposure_usd": cap.get("open_exposure_usd"),
        "open_positions_count": cap.get("open_positions", ledger_stats.get("open_positions")),
        "return_pct": cap.get("return_pct", cap.get("total_return_pct")),
        "arb_realized_pnl_usd": cap.get("arb_realized_pnl_usd"),
        "source_ts": status.get("ts"),
        "ticks": status.get("ticks"),
    }


def _open_trades_from_ledger(ledger: dict) -> list[dict]:
    rows: list[dict] = []
    for pos in ledger.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        rows.append({
            "event_id": pos.get("event_id") or pos.get("market_id") or "—",
            "side": pos.get("side", "—"),
            "entry_price": pos.get("entry_price") or pos.get("fill_price"),
            "size_usd": pos.get("size_usd") or pos.get("notional_usd"),
            "token_id": (pos.get("token_id") or pos.get("up_token_id") or pos.get("down_token_id") or "—"),
            "time_boundary": pos.get("close_ts") or pos.get("time_boundary") or "—",
            "status": pos.get("status", "open"),
            "series_slug": pos.get("series_slug", "—"),
        })
    return rows


@dataclass
class DiscoveryCandidate:
    slug: str
    token_id: str
    ask: float
    verdict: str
    title: str = ""

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "token_id": self.token_id[:32],
            "ask": self.ask,
            "verdict": self.verdict,
            "title": self.title[:80],
        }


def _best_ask(token_id: str) -> float | None:
    try:
        book = _fetch_with_429_retry(f"{CLOB}/book?{urllib.parse.urlencode({'token_id': token_id})}")
        asks = book.get("asks") or []
        if not asks:
            return None
        return float(asks[0].get("price") if isinstance(asks[0], dict) else asks[0][0])
    except Exception as exc:
        _STATE["scan_errors"].append(f"book:{token_id[:12]}:{exc}")
        return None


def discover_crypto_candidates(skill: SkillThresholds, *, limit: int = 12) -> list[DiscoveryCandidate]:
    """Lightweight Gamma + CLOB scan using SKILL_ANALYSIS price bands."""
    out: list[DiscoveryCandidate] = []
    try:
        url = (
            f"{GAMMA}/events?active=true&closed=false&limit={limit}"
            "&tag_id=21&order=volume24hr&ascending=false"
        )
        events = _fetch_with_429_retry(url)
    except Exception as exc:
        _STATE["scan_errors"].append(f"gamma:{exc}")
        return out

    for ev in events if isinstance(events, list) else []:
        for mkt in ev.get("markets") or []:
            if not mkt.get("active") or mkt.get("closed"):
                continue
            tokens = mkt.get("clobTokenIds") or mkt.get("clob_token_ids")
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except json.JSONDecodeError:
                    continue
            if not tokens:
                continue
            token_id = str(tokens[0])
            ask = _best_ask(token_id)
            if ask is None:
                continue
            verdict = skill.classify_ask(ask, tail_breakthrough=False)
            if verdict.startswith("PROCEED"):
                out.append(DiscoveryCandidate(
                    slug=str(mkt.get("slug") or ev.get("slug") or ""),
                    token_id=token_id,
                    ask=ask,
                    verdict=verdict,
                    title=str(mkt.get("question") or ev.get("title") or ""),
                ))
            if len(out) >= 5:
                return out
    return out


def _load_existing_wake_count() -> int:
    if not MEMORY_PATH.exists():
        return 0
    try:
        for line in MEMORY_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("- **wake_count:**"):
                return int(line.split(":")[-1].strip())
    except Exception:
        pass
    return 0


def write_memory(
    *,
    skill: SkillThresholds,
    wallet: dict,
    open_trades: list[dict],
    candidates: list[DiscoveryCandidate],
    run_id: str,
) -> None:
    """Skill §4 — explicit open trades + wallet balances before shutdown."""
    wake_count = _load_existing_wake_count() + 1
    lines = [
        "# Bot-1 Loop Memory (MEMORY.md)",
        "",
        "_Disk-bound cloud loop state. Read on wake; updated each GitHub Actions cycle. PAPER ONLY._",
        "",
        "## Meta",
        f"- **last_wake:** {_utc_now()}",
        f"- **wake_count:** {wake_count}",
        f"- **run_id:** {run_id}",
        f"- **schema:** bot1_cloud_memory/1.0",
        f"- **skill_hash:** {skill.content_hash or '—'}",
        "",
        "## Active constraints",
        "- PAPER ONLY — live_trading_enabled must stay false",
        "- Loop Engineering architecture LOCKED — ask operator before lane/optimizer/MEMORY-schema changes",
        "- Thresholds loaded from SKILL_ANALYSIS.md (not in-context guessing)",
        "- Maker-checker on VPS — cloud cycle observes + persists state only",
        "",
        "## Skill thresholds (SKILL_ANALYSIS.md)",
        "",
        "| key | value |",
        "|-----|-------|",
    ]
    for k, v in skill.as_dict().items():
        if k in ("loaded", "path", "content_hash"):
            continue
        lines.append(f"| `{k}` | {v} |")
    lines += [
        "",
        "## Wallet balances (paper)",
        "",
        "| field | value |",
        "|-------|-------|",
    ]
    for k, v in wallet.items():
        lines.append(f"| `{k}` | {v} |")
    lines += [
        "",
        "## Open trades",
        "",
        "| event_id | side | entry_price | size_usd | token_id | time_boundary | status |",
        "|----------|------|-------------|----------|----------|---------------|--------|",
    ]
    if open_trades:
        for t in open_trades:
            lines.append(
                "| {event_id} | {side} | {entry_price} | {size_usd} | {token_id} | {time_boundary} | {status} |".format(
                    event_id=str(t.get("event_id", "—"))[:28],
                    side=t.get("side", "—"),
                    entry_price=t.get("entry_price", "—"),
                    size_usd=t.get("size_usd", "—"),
                    token_id=str(t.get("token_id", "—"))[:24],
                    time_boundary=str(t.get("time_boundary", "—"))[:20],
                    status=t.get("status", "open"),
                )
            )
    else:
        lines.append("| — | — | — | — | — | — | none |")
    lines += [
        "",
        "## Discovery candidates (this cycle)",
        "",
        "| slug | ask | verdict | token_id |",
        "|------|-----|---------|----------|",
    ]
    if candidates:
        for c in candidates:
            d = c.as_dict()
            lines.append(
                f"| {d['slug'][:32]} | {d['ask']:.4f} | {d['verdict']} | {d['token_id']} |"
            )
    else:
        lines.append("| — | — | — | — |")
    errs = _STATE.get("scan_errors") or []
    if errs:
        lines += ["", "## Scan errors", ""]
        for e in errs[-5:]:
            lines.append(f"- {e}")
    lines.append("")
    MEMORY_PATH.write_text("\n".join(lines), encoding="utf-8")
    _log(f"wrote {MEMORY_PATH}")


def write_logs() -> None:
    LOGS_PATH.write_text("\n".join(_LOG_LINES) + "\n", encoding="utf-8")


def _persist_shutdown() -> None:
    """Always flush LOGS.txt; MEMORY.md written in main before this if successful."""
    try:
        write_logs()
    except Exception as exc:
        print(f"failed to write logs: {exc}", file=sys.stderr)


atexit.register(_persist_shutdown)


def run_cycle(*, vps_url: str, discovery: bool) -> int:
    skill = SkillThresholds.load(REPO_ROOT)
    _STATE["skill"] = skill.as_dict()
    _log(f"SKILL_ANALYSIS loaded={skill.loaded} path={skill.path} hash={skill.content_hash}")
    _log(
        f"thresholds sweet=[{skill.sweet_min},{skill.sweet_max}] "
        f"tail<{skill.tail_max} depth>={skill.min_depth_usd}"
    )

    status, ledger = pull_vps_state(vps_url)
    wallet = _wallet_from_status(status, ledger)
    open_trades = _open_trades_from_ledger(ledger)
    _STATE["wallet"] = wallet
    _STATE["open_trades"] = open_trades
    _log(
        f"VPS wallet on_hand={wallet.get('on_hand_capital_usd')} "
        f"open_positions={wallet.get('open_positions_count')}"
    )
    _log(f"open trades pulled: {len(open_trades)}")

    candidates: list[DiscoveryCandidate] = []
    if discovery:
        candidates = discover_crypto_candidates(skill)
        _log(f"discovery candidates: {len(candidates)}")
        for c in candidates:
            _log(f"  {c.verdict} {c.slug} ask={c.ask:.4f}")

    run_id = f"cloud-{_utc_now().replace(' ', '_').replace(':', '')}"
    write_memory(
        skill=skill,
        wallet=wallet,
        open_trades=open_trades,
        candidates=candidates,
        run_id=run_id,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot-1 cloud loop — SKILL_ANALYSIS + MEMORY.md")
    ap.add_argument("--vps-url", default=os.getenv("BOT1_VPS_URL", DEFAULT_VPS))
    ap.add_argument("--no-discovery", action="store_true", help="Skip Gamma/CLOB scan")
    args = ap.parse_args()
    try:
        return run_cycle(vps_url=args.vps_url, discovery=not args.no_discovery)
    except Exception as exc:
        _log(f"FATAL: {exc}")
        # Still persist partial state for operator visibility
        skill = SkillThresholds.load(REPO_ROOT)
        write_memory(
            skill=skill,
            wallet=_STATE.get("wallet") or {"error": str(exc)},
            open_trades=_STATE.get("open_trades") or [],
            candidates=[],
            run_id="cloud-error",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
