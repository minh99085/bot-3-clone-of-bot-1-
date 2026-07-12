#!/usr/bin/env python3
"""Write a short plain-English cycle summary for the operator."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LATEST = ROOT / "vps_full_reports" / "latest"
STATUS = LATEST / "btc_pulse_status.json"
STATE = Path(__file__).resolve().parent / "state.json"
OUT = LATEST / "CYCLE_SUMMARY.md"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _pct(v, digits=0) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _money(v) -> str:
    if v is None:
        return "—"
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_utc(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(iso)


def _verdict_plain(verdict: str | None, issues: list) -> str:
    v = str(verdict or "").lower()
    codes = [i.get("code") if isinstance(i, dict) else i for i in (issues or [])]
    if v == "healthy":
        return "All good — no fixes needed this cycle."
    if v == "blocked":
        return "Stopped — serious problem found. Check issues below."
    if v == "deploy":
        return "Changes were deployed to the VPS."
    if "trade_starvation" in codes or "trade_starvation_streak" in codes:
        return ("ABNORMAL — bot is running but no new trades for hours. "
                "Relax over-tight gates; do NOT tighten WR rules on stale ledger.")
    if "up_side_bleed" in codes:
        return "Issues found — UP trades still lose money. More UP blocks may have been added."
    if v == "issues":
        return "Issues found — bot still running, but tuning may be needed."
    return f"Result: {verdict or 'unknown'}"


def _grade_overall(score: float | None) -> str:
    if score is None:
        return "—"
    s = float(score)
    if s >= 80:
        return "A"
    if s >= 70:
        return "B"
    if s >= 60:
        return "C"
    if s >= 50:
        return "D"
    return "F"


def build_summary() -> str:
    st = _load(STATE)
    s = _load(STATUS)
    capital = s.get("capital") or {}
    ledger = s.get("ledger") or {}
    arb = s.get("arbitrage") or {}
    tv = s.get("tradingview") or {}
    stop = (s.get("stop_conditions") or {}).get("strategies") or {}
    by = s.get("by_market_series") or {}

    cycle = st.get("cycle", "?")
    last_eval = st.get("last_eval_at")
    last_verdict = st.get("last_verdict")
    soak_until = st.get("soak_until")
    last_fixes = st.get("last_fixes") or []
    hist = st.get("history") or []
    last_entry = hist[-1] if hist else {}
    issue_codes = last_entry.get("issue_codes") or []

    mtf = tv.get("tradingview_mtf_confirmation") or {}
    mtf_verdict = (
        mtf.get("confirm_3tf") or mtf.get("confirm_mtf")
        or mtf.get("confirm") or "none"
    )
    fresh = mtf.get("trend_fresh_count")
    mtf_n = mtf.get("mtf_count") or len(mtf.get("mtf_timeframes") or [])

    m5 = by.get("5m") or {}
    m15 = by.get("15m") or {}

    dir_halted = (stop.get("directional") or {}).get("halted", False)
    arb_halted = (stop.get("arbitrage") or {}).get("halted", False)
    halted_txt = "No — bot is running"
    if dir_halted or arb_halted:
        halted_txt = f"Yes — directional={dir_halted}, arbitrage={arb_halted}"

    score_hist = _load(LATEST / "btc_pulse_score_history.json")
    overall_score = None
    if isinstance(score_hist, list) and score_hist:
        overall_score = (score_hist[-1] or {}).get("overall")
    elif isinstance(score_hist, dict):
        rows = score_hist.get("history") or score_hist.get("scores") or []
        if rows:
            overall_score = (rows[-1] or {}).get("overall")

    epoch = _load(LATEST / "REPORT_EPOCH.json")
    if not epoch.get("utc"):
        light = _load(LATEST / "btc_pulse_light_report.json")
        epoch = light.get("report_epoch") or {}

    lines = [
        "# Bot cycle summary (plain English)",
        "",
        f"_Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
    ]
    if epoch.get("utc"):
        lines += [
            f"_Trading report baseline: **{epoch.get('utc')}**"
            f" (token `{epoch.get('token') or '—'}`) — metrics below are since this point._",
            "",
        ]
    lines += [
        "## Last cycle",
        "",
        f"| | |",
        f"|---|---|",
        f"| **Cycle #** | {cycle} |",
        f"| **Checked at** | {_fmt_utc(last_eval)} |",
        f"| **Result** | **{last_verdict or '—'}** |",
        f"| **What it means** | {_verdict_plain(last_verdict, issue_codes)} |",
        f"| **Next check after** | {_fmt_utc(soak_until)} |",
        "",
    ]

    if issue_codes:
        lines += ["**Issues flagged:** " + ", ".join(str(c) for c in issue_codes), ""]

    if last_fixes:
        lines += ["**Fixes applied:**", ""]
        for fix in last_fixes:
            lines.append(f"- {fix}")
        lines.append("")

    lines += [
        "## How the bot is doing now",
        "",
        f"| | |",
        f"|---|---|",
        f"| **Mode** | {'Paper only (fake money)' if s.get('paper_only', True) else 'LIVE'} |",
        f"| **Started with** | $500.00 |",
        f"| **Total now** | {_money(capital.get('total_on_hand_usd'))} "
        f"({capital.get('total_return_pct', '—')}% return) |",
        f"| **Arb profit** | {_money(capital.get('arb_realized_pnl_usd'))} "
        f"({arb.get('executed', 0)} trades) |",
        f"| **Directional profit** | {_money(capital.get('realized_pnl_usd'))} |",
        f"| **Win rate** | {_pct(ledger.get('win_rate'), 1)} "
        f"({ledger.get('settled', ledger.get('trades', 0))} settled trades) |",
        f"| **UP win rate** | {_pct(ledger.get('win_rate_up'), 1)} |",
        f"| **DOWN win rate** | {_pct(ledger.get('win_rate_down'), 1)} |",
        f"| **Bot stopped?** | {halted_txt} |",
        f"| **Overall grade** | {_grade_overall(overall_score)} "
        f"({overall_score if overall_score is not None else '—'}/100) |",
        "",
        "### 5m vs 15m (recent)",
        "",
        f"| Market | Trades | Win rate | PnL |",
        f"|--------|--------|----------|-----|",
        f"| **15m** | {m15.get('settled', '—')} | {_pct(m15.get('win_rate'), 1)} "
        f"| {_money(m15.get('pnl_usd'))} |",
        f"| **5m** | {m5.get('settled', '—')} | {_pct(m5.get('win_rate'), 1)} "
        f"| {_money(m5.get('pnl_usd'))} |",
        "",
        "### TradingView (INDEX:BTCUSD)",
        "",
        f"- Alerts received: **{tv.get('tradingview_alerts_valid', 0)}**",
        f"- 5-chart trend: **{mtf_verdict}** ({fresh or '—'}/{mtf_n or 5} fresh)",
        "",
        "## Quick verdict",
        "",
    ]

    total = float(capital.get("total_return_pct") or 0)
    wr_up = ledger.get("win_rate_up")
    arb_pnl = float(capital.get("arb_realized_pnl_usd") or 0)

    good, bad = [], []
    if total > 0:
        good.append(f"Making money on paper (+{total:.1f}%)")
    if arb_pnl > 40:
        good.append("Arbitrage is doing most of the work")
    if ledger.get("win_rate_down") and float(ledger["win_rate_down"]) >= 0.65:
        good.append("DOWN trades work well")
    if not dir_halted and not arb_halted:
        good.append("Bot is running normally")
    if wr_up is not None and float(wr_up) <= 0.52:
        bad.append("UP trades still weak (coin-flip or worse)")
    if "up_side_bleed" in issue_codes:
        bad.append("Cycle flagged UP-side losses")
    if int(tv.get("tradingview_alerts_valid") or 0) < 10:
        bad.append("Few TradingView alerts so far")

    if good:
        lines.append("**Good:** " + "; ".join(good) + ".")
        lines.append("")
    if bad:
        lines.append("**Watch:** " + "; ".join(bad) + ".")
        lines.append("")

    lines += [
        "---",
        "",
        "_Auto-generated after each `/pulse-babysit` cycle. "
        "Full report: `report.md` / `report.docx` in this folder._",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    text = build_summary()
    LATEST.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())