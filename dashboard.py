"""Hermes v2 dashboard — 10-lane BTC15 paired experiment, $20k fleet."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from hermes.logging_config import setup_logging

    setup_logging("dashboard")
except Exception:
    pass

from hermes.dashboard_data import (
    FLEET_BANKROLL,
    FLEET_INSTANCE_COUNT,
    PER_INSTANCE_BANKROLL,
    bandit_states_all,
    fleet_equity_curve,
    fleet_summary,
    fleet_trade_history,
    instance_cards,
    instance_trade_history,
    lane_scoreboard,
    load_state,
)

st.set_page_config(
    page_title="Bot 3 · 10 Lanes",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

REFRESH_SEC = int(os.environ.get("DASHBOARD_REFRESH_SEC", "300"))
st.markdown(
    f'<meta http-equiv="refresh" content="{REFRESH_SEC}">',
    unsafe_allow_html=True,
)

ROLE_PILL = {
    "control": ("#38bdf8", "control"),
    "experiment": ("#4ade80", "experiment"),
    "neg_control": ("#f87171", "neg control"),
    "null": ("#94a3b8", "null"),
}

st.markdown(
    """
<style>
    .main-header { font-size: 2.0rem; font-weight: 700; margin-bottom: 0.25rem; }
    .sub-header { color: #888; font-size: 1rem; margin-bottom: 1.25rem; }
    .fleet-pill {
        display: inline-block;
        background: #1a1a2e;
        border: 1px solid #333;
        border-radius: 8px;
        padding: 0.35rem 0.75rem;
        margin-right: 0.5rem;
        margin-bottom: 0.35rem;
        font-size: 0.85rem;
    }
    .instance-card {
        background: #1a1a2e;
        border: 1px solid #333;
        border-left: 3px solid var(--accent, #38bdf8);
        border-radius: 12px;
        padding: 0.85rem 0.95rem;
        margin-bottom: 0.65rem;
        height: 100%;
    }
    .instance-title { font-size: 0.95rem; font-weight: 600; margin-bottom: 0.1rem; }
    .instance-sub {
        color: #888; font-size: 0.72rem; margin-bottom: 0.45rem;
        line-height: 1.25; min-height: 2.1em;
    }
    .role-pill {
        display: inline-block;
        font-size: 0.65rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        border-radius: 4px;
        padding: 0.1rem 0.4rem;
        margin-bottom: 0.35rem;
        border: 1px solid;
    }
    .metric-row { display: flex; justify-content: space-between; font-size: 0.82rem; margin: 0.15rem 0; }
    .metric-label { color: #aaa; }
    .metric-value { font-weight: 600; }
    .positive { color: #00c853; }
    .negative { color: #ff5252; }
    .neutral { color: #888; }
    div[data-testid="stMetricValue"] { font-size: 1.25rem; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_data(ttl=REFRESH_SEC)
def cached_fleet():
    return {
        "state": load_state(),
        "fleet": fleet_summary(),
        "instances": instance_cards(),
        "scoreboard": lane_scoreboard(),
        "equity": fleet_equity_curve(),
        "recent_trades": fleet_trade_history(50),
        "bandits": bandit_states_all(),
    }


def pnl_class(value: float) -> str:
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def fmt_pnl(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}${value:,.2f}"


def render_lane_card(card: dict) -> None:
    pnl = card["pnl"]
    pnl_cls = pnl_class(pnl)
    wr = card.get("win_rate") or 0.0
    accent = card.get("accent") or "#38bdf8"
    role = card.get("role") or "experiment"
    role_color, role_label = ROLE_PILL.get(role, ROLE_PILL["experiment"])
    st.markdown(
        f"""
<div class="instance-card" style="--accent: {accent}">
  <div class="role-pill" style="color:{role_color};border-color:{role_color}">{role_label}</div>
  <div class="instance-title">{card['label']}</div>
  <div class="instance-sub">{card['subtitle']}</div>
  <div class="metric-row"><span class="metric-label">Equity</span>
    <span class="metric-value">${card['equity']:,.2f}</span></div>
  <div class="metric-row"><span class="metric-label">P&L</span>
    <span class="metric-value {pnl_cls}">{fmt_pnl(pnl)}</span></div>
  <div class="metric-row"><span class="metric-label">Win rate</span>
    <span class="metric-value">{wr:.1%}</span></div>
  <div class="metric-row"><span class="metric-label">Trades</span>
    <span class="metric-value">{card['trades']} ({card['wins']}W/{card['losses']}L)</span></div>
  <div class="metric-row"><span class="metric-label">Open</span>
    <span class="metric-value">{card['open_positions']}</span></div>
  <div class="metric-row"><span class="metric-label">Status</span>
    <span class="metric-value">{card['status']}</span></div>
</div>
""",
        unsafe_allow_html=True,
    )


def main() -> None:
    data = cached_fleet()
    fleet = data["fleet"]
    instances = data["instances"]
    scoreboard = data["scoreboard"]
    fleet_eq = data["equity"]
    bandits = data["bandits"]

    st.sidebar.title("Bot 3")
    st.sidebar.markdown("**10-lane BTC15 experiment**")
    st.sidebar.markdown(
        f"**${PER_INSTANCE_BANKROLL:,.0f}** each · **${FLEET_BANKROLL:,.0f}** total"
    )
    st.sidebar.markdown("---")
    st.sidebar.markdown("**Lanes**")
    for inst in instances:
        active = inst["status"] in ("active", "watching")
        mark = "●" if active else "○"
        st.sidebar.markdown(
            f"{mark} **{inst['label']}** — ${inst['equity']:,.0f}"
        )
    st.sidebar.markdown("---")
    if st.sidebar.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()
    st.sidebar.caption(f"Auto-refresh every {REFRESH_SEC}s")

    st.markdown(
        '<p class="main-header">Hermes v2 · 10-Lane BTC15 Experiment</p>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<p class="sub-header">'
        "Paired paper fleet on btc-updown-15m · $2k × 10 = $20k · "
        "rank by ΔPnL vs random null"
        "</p>",
        unsafe_allow_html=True,
    )

    fleet_pnl = fleet["total_pnl"]
    pnl_delta = fmt_pnl(fleet_pnl)
    pnl_delta_color = "normal" if fleet_pnl >= 0 else "inverse"

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Fleet bankroll", f"${fleet['fleet_bankroll']:,.0f}")
    c2.metric(
        "Fleet equity",
        f"${fleet['fleet_equity']:,.2f}",
        delta=pnl_delta,
        delta_color=pnl_delta_color,
    )
    c3.metric("Fleet P&L", fmt_pnl(fleet_pnl))
    c4.metric("Fleet win rate", f"{fleet['win_rate']:.1%}")
    c5.metric("Total trades", f"{fleet['total_trades']}")
    c6.metric("Open positions", f"{fleet['open_positions']}")

    shared = scoreboard.get("n_shared_windows", 0)
    st.markdown(
        f'<span class="fleet-pill">{FLEET_INSTANCE_COUNT} lanes</span>'
        f'<span class="fleet-pill">${PER_INSTANCE_BANKROLL:,.0f} / lane</span>'
        f'<span class="fleet-pill">{fleet["wins"]}W / {fleet["losses"]}L settled</span>'
        f'<span class="fleet-pill">{shared} shared windows</span>',
        unsafe_allow_html=True,
    )

    recent = data.get("recent_trades") or []
    st.markdown("---")
    with st.expander(f"Last 50 trades ({len(recent)})", expanded=False):
        if recent:
            df_recent = pd.DataFrame(recent)
            show_cols = [
                c
                for c in (
                    "time",
                    "lane",
                    "slug",
                    "direction",
                    "size",
                    "entry",
                    "exit",
                    "won",
                    "pnl",
                    "status",
                    "entry_source",
                )
                if c in df_recent.columns
            ]
            st.dataframe(
                df_recent[show_cols],
                use_container_width=True,
                hide_index=True,
                height=min(420, 48 + 28 * len(df_recent)),
            )
        else:
            st.info("No trades yet across the fleet.")

    st.subheader("Fleet equity ($20,000 baseline)")
    if len(fleet_eq) > 1:
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=[p["ts"] for p in fleet_eq if p["ts"] != "start"],
                y=[p["equity"] for p in fleet_eq if p["ts"] != "start"],
                mode="lines",
                name="Fleet equity",
                line=dict(color="#00c853", width=2),
                fill="tozeroy",
                fillcolor="rgba(0,200,83,0.08)",
            )
        )
        fig.add_hline(
            y=FLEET_BANKROLL,
            line_dash="dash",
            line_color="#666",
            annotation_text=f"Start ${FLEET_BANKROLL:,.0f}",
        )
        fig.update_layout(
            height=320,
            margin=dict(l=0, r=0, t=30, b=0),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#ccc"),
            xaxis=dict(showgrid=False),
            yaxis=dict(showgrid=True, gridcolor="#333", tickformat="$,.0f"),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info(
            "No settlement history yet — fleet equity chart appears after first settled trades."
        )

    st.markdown("---")
    st.subheader("Lane overview")
    # 2×5 grid
    for row_start in (0, 5):
        cols = st.columns(5)
        for col, card in zip(cols, instances[row_start : row_start + 5]):
            with col:
                render_lane_card(card)

    ret_pct = fleet_pnl / FLEET_BANKROLL * 100 if FLEET_BANKROLL else 0.0
    st.caption(
        f"Fleet aggregate: ${fleet['fleet_equity']:,.2f} equity · "
        f"{ret_pct:+.2f}% return · "
        f"{fleet['instances_with_data']}/{FLEET_INSTANCE_COUNT} lanes reporting"
    )

    st.markdown("---")
    st.subheader("Paired scoreboard vs null")
    st.caption(
        "All lanes trade the same btc-updown-15m windows. "
        "ΔPnL vs null cancels market luck — promotion signal is beat lane09 (random)."
    )
    rows = scoreboard.get("rows") or []
    if rows:
        df = pd.DataFrame(rows)
        display = df.rename(
            columns={
                "label": "Lane",
                "role": "Role",
                "n": "N",
                "wr": "WR",
                "pnl": "PnL $",
                "avg_entry": "Avg entry",
                "n_paired": "N paired",
                "delta_vs_null": "ΔPnL vs null",
            }
        )[
            [
                "Lane",
                "Role",
                "N",
                "WR",
                "PnL $",
                "Avg entry",
                "N paired",
                "ΔPnL vs null",
            ]
        ]
        st.dataframe(
            display.style.format(
                {
                    "WR": "{:.1%}",
                    "PnL $": "{:+.2f}",
                    "Avg entry": "{:.3f}",
                    "ΔPnL vs null": "{:+.2f}",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )
        for note in scoreboard.get("notes") or []:
            st.warning(note)
    else:
        st.info("No lane ledgers yet — scoreboard fills as settlements arrive.")

    st.markdown("---")
    st.subheader("Lane trade history")
    options = {f"{c['label']} (${c['equity']:,.0f})": c["id"] for c in instances}
    choice = st.selectbox("Select lane", list(options.keys()))
    iid = options[choice]
    card = next(c for c in instances if c["id"] == iid)
    hist = instance_trade_history(iid)
    ic1, ic2, ic3, ic4 = st.columns(4)
    ic1.metric("Bankroll", f"${card['bankroll']:,.0f}")
    ic2.metric("Equity", f"${card['equity']:,.2f}")
    ic3.metric("Win rate", f"{(card.get('win_rate') or 0):.1%}")
    ic4.metric("Trades", card["trades"])
    if hist:
        st.dataframe(pd.DataFrame(hist), use_container_width=True, hide_index=True)
    else:
        st.info(f"No trades yet for {card['label']}.")

    st.markdown("---")
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Bandit state (per lane)")
        if bandits:
            for bid, bstate in bandits.items():
                with st.expander(f"Lane `{bid}`"):
                    st.json(bstate)
        else:
            st.info("No bandit state files yet.")

    with col_b:
        st.subheader("Experiment config")
        st.json(
            {
                "market_filter": "btc15",
                "series": "btc_updown_15m",
                "lanes": FLEET_INSTANCE_COUNT,
                "per_lane_bankroll": PER_INSTANCE_BANKROLL,
                "fleet_bankroll": FLEET_BANKROLL,
                "null_lane": scoreboard.get("null_lane"),
                "ranking": "paired ΔPnL vs random_null",
            }
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    st.caption(f"Last loaded: {now} · refresh TTL {REFRESH_SEC}s")


if __name__ == "__main__":
    main()
