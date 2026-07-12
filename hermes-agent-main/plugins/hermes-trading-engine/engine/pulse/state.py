"""STATE.md — human-readable loop memory snapshot (Loop Engineering #2).

Emitted on each engine persist alongside LESSONS.md: capital, open positions, active strategies,
verifiable stop states, and active lessons. Pure (dict -> markdown); no side effects.
"""

from __future__ import annotations

import datetime
from typing import Optional


def _ts(epoch: Optional[float]) -> str:
    if not epoch:
        return "—"
    return datetime.datetime.fromtimestamp(float(epoch), datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


def build_state_md(*, status: dict, ledger: dict, stop_conditions: Optional[dict] = None,
                   lessons: Optional[dict] = None) -> str:
    status = status or {}
    ledger = ledger or {}
    stop_conditions = stop_conditions or {}
    lessons = lessons or {}
    out: list = []

    cap = status.get("capital") or {}
    led = status.get("ledger") or ledger.get("stats") or {}
    cfg = status.get("config") or {}
    stops = stop_conditions.get("strategies") or {}

    out.append("# Hermes BTC Pulse — STATE (auto-generated snapshot)\n")
    out.append("_Updated each persist. Human-readable loop memory. PAPER ONLY._\n")
    out.append("- **ticks:** %s · **last tick:** %s\n" % (status.get("ticks"), _ts(status.get("ts"))))

    out.append("## Capital\n")
    out.append("- **starting:** $%s · **on-hand (directional):** $%s · **return:** %s%%"
               % (cap.get("starting_capital_usd"), cap.get("on_hand_capital_usd"),
                  cap.get("return_pct")))
    out.append("- **total on-hand:** $%s · **total return:** %s%%"
               % (cap.get("total_on_hand_usd"), cap.get("total_return_pct")))
    out.append("- **open exposure:** $%s (%s positions)\n"
               % (cap.get("open_exposure_usd"), cap.get("open_positions")))

    out.append("## Active strategies\n")
    dir_halted = (stops.get("directional") or {}).get("halted")
    grok_mode = (status.get("grok_decider") or {}).get("mode") or cfg.get("grok_decider_mode")
    out.append("- **directional:** enabled · halted=%s · settled=%s · WR=%s · PF=%s · PnL=$%s"
               % (dir_halted, led.get("settled"), led.get("win_rate"), led.get("profit_factor"),
                  led.get("realized_pnl_usd")))
    out.append("- **grok decider:** mode=%s · affects_trading=%s"
               % (grok_mode, (status.get("grok_decider") or {}).get("affects_trading")))
    ver = status.get("verifier") or {}
    out.append("- **verifier (maker-checker):** enabled=%s · approve_rate=%s\n"
               % (ver.get("enabled"), ver.get("approve_rate")))

    out.append("## Verifiable stop conditions\n")
    for name in ("directional",):
        st = stops.get(name) or {}
        metrics = st.get("metrics") or {}
        out.append("- **%s:** halted=%s · reasons=%s · metrics=%s"
                   % (name, st.get("halted"), st.get("reasons"), metrics))
    out.append("")

    out.append("## Open positions (directional)\n")
    positions = [p for p in (ledger.get("positions") or []) if p.get("status") == "open"][:8]
    if positions:
        for p in positions:
            mode = (p.get("research") or {}).get("entry_mode", "—")
            out.append("- %s **%s** @ %s · $%s · mode=%s"
                       % ((p.get("title") or "")[-22:], p.get("side"), p.get("entry_price"),
                          p.get("size_usd"), mode))
    else:
        out.append("_none_\n")

    out.append("\n## Active lessons\n")
    recent = (lessons.get("recent") or [])[-8:]
    if recent:
        for ln in recent:
            out.append("- [`%s`] %s" % (ln.get("kind"), ln.get("rule")))
    else:
        out.append("_none_\n")

    out.append("\n## Gates (restrict-only)\n")
    tv = status.get("tradingview") or {}
    cg = tv.get("context_gate") or {}
    out.append("- **context_gate:** enabled=%s · blocked=%s · reasons=%s"
               % (cg.get("enabled"), cg.get("blocked"), cg.get("block_reasons")))
    sel = status.get("learned_selectivity_gate") or {}
    out.append("- **selectivity_gate:** enabled=%s · rejected=%s"
               % (sel.get("enabled"), sel.get("rejected")))
    out.append("- **readiness:** %s\n" % ((status.get("readiness") or {}).get("status")))

    return "\n".join(out) + "\n"