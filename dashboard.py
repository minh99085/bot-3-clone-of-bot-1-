"""Hermes v2 fleet dashboard — 5 isolated instances, $10k total bankroll."""

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
    instance_cards,
    instance_trade_history,
    load_state,
    scoped_market_cards,
)
from models.config import load_enhanced_config

st.set_page_config(
    page_title="Hermes v2 Fleet",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

REFRESH_SEC = int(os.environ.get("DASHBOARD_REFRESH_SEC", "300"))
st.markdown(
    f'<meta http-equiv="refresh" content="{REFRESH_SEC}">',
    unsafe_allow_html=True,
)

st.markdown(
    """
<style>
    .main-header { font-size: 2.2rem; font-weight: 700; margin-bottom: 0.25rem; }
    .sub-header { color: #888; font-size: 1rem; margin-bottom: 1.5rem; }
    .fleet-pill {
        display: inline-block;
        background: #1a1a2e;
        border: 1px solid #333;
        border-radius: 8px;
        padding: 0.35rem 0.75rem;
        margin-right: 0.5rem;
        font-size: 0.85rem;
    }
    .instance-card {
        background: #1a1a2e;
        border: 1px solid #333;
        border-radius: 12px;
        padding: 1rem 1.1rem;
        margin-bottom: 0.75rem;
        height: 100%;
    }
    .instance-title { font-size: 1.05rem; font-weight: 600; margin-bottom: 0.15rem; }
    .instance-sub { color: #888; font-size: 0.8rem; margin-bottom: 0.6rem; }
    .metric-row { display: flex; justify-content: space-between; font-size: 0.88rem; margin: 0.2rem 0; }
    .metric-label { color: #aaa; }
    .metric-value { font-weight: 600; }
    .positive { color: #00c853; }
    .negative { color: #ff5252; }
    .neutral { color: #888; }
    div[data-testid="stMetricValue"] { font-size: 1.35rem; }
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
        "scoped": scoped_market_cards(),
        "equity": fleet_equity_curve(),
        "bandits": bandit_states_all(),
        "config": load_enhanced_config().model_dump(),
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


def render_instance_card(card: dict) -> None:
    pnl = card["pnl"]
    pnl_cls = pnl_class(pnl)
    wr = card.get("win_rate") or 0.0
    st.markdown(
        f"""
<div class="instance-card">
  <div class="instance-title">{card['label']}</div>
  <div class="instance-sub">{card['subtitle']}</div>
  <div class="metric-row"><span class="metric-label">Bankroll</span>
    <span class="metric-value">${card['bankroll']:,.0f}</span></div>
  <div class="metric-row"><span class="metric-label">Equity</span>
    <span class="metric-value">${card['equity']:,.2f}</span></div>
  <div class="metric-row"><span class="metric-label">P&L</span>
    <span class="metric-value {pnl_cls}">{fmt_pnl(pnl)}</span></div>
  <div class="metric-row"><span class="metric-label">Win rate</span>
    <span class="metric-value">{wr:.1%}</span></div>
  <div class="metric-row"><span class="metric-label">Trades</span>
    <span class="metric-value">{card['trades']} ({card['wins']}W / {card['losses']}L)</span></div>
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
    scoped = data["scoped"]
    fleet_eq = data["equity"]
    bandits = data["bandits"]
    cfg = data["config"]

    st.sidebar.title("Hermes v2 Fleet")
    st.sidebar.markdown("**5 isolated paper instances**")
    st.sidebar.markdown(
        f"**${PER_INSTANCE_BANKROLL:,.0f}** each · **${FLEET_BANKROLL:,.0f}** total"
    )
    st.sidebar.markdown("---")
    st.sidebar.markdown("**Instances**")
    for inst in instances:
        dot = "🟢" if inst["status"] in ("active", "watching") else "⚪"
        st.sidebar.markdown(f"{dot} **{inst['label']}** — ${inst['equity']:,.0f}")
    st.sidebar.markdown("---")
    if st.sidebar.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()
    st.sidebar.caption(f"Auto-refresh every {REFRESH_SEC}s")

    st.markdown(
        '<p class="main-header">Hermes v2 · 5-Instance Paper Fleet</p>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<p class="sub-header">BTC / ETH / SOL lanes + rotator · moderate filter · paper only</p>',
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

    st.markdown(
        f'<span class="fleet-pill">{FLEET_INSTANCE_COUNT} instances</span>'
        f'<span class="fleet-pill">${PER_INSTANCE_BANKROLL:,.0f} per instance</span>'
        f'<span class="fleet-pill">{fleet["wins"]}W / {fleet["losses"]}L settled</span>',
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.subheader("Fleet equity ($10,000 baseline)")
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
            height=340,
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
        st.info("No settlement history yet — fleet equity chart appears after first settled trades.")

    st.markdown("---")
    st.subheader("Instance overview")

    row1 = st.columns(3)
    for col, card in zip(row1, instances[:3]):
        with col:
            render_instance_card(card)

    row2 = st.columns(3)
    with row2[0]:
        render_instance_card(instances[3])
    with row2[1]:
        render_instance_card(instances[4])
    with row2[2]:
        ret_pct = fleet_pnl / FLEET_BANKROLL * 100 if FLEET_BANKROLL else 0.0
        st.markdown(
            f"""
<div class="instance-card">
  <div class="instance-title">Fleet aggregate</div>
  <div class="instance-sub">Sum of all 5 instance ledgers</div>
  <div class="metric-row"><span class="metric-label">Starting capital</span>
    <span class="metric-value">${FLEET_BANKROLL:,.0f}</span></div>
  <div class="metric-row"><span class="metric-label">Current equity</span>
    <span class="metric-value">${fleet['fleet_equity']:,.2f}</span></div>
  <div class="metric-row"><span class="metric-label">Return</span>
    <span class="metric-value {pnl_class(fleet_pnl)}">{ret_pct:+.2f}%</span></div>
  <div class="metric-row"><span class="metric-label">Instances reporting</span>
    <span class="metric-value">{fleet['instances_with_data']}/{FLEET_INSTANCE_COUNT}</span></div>
</div>
""",
            unsafe_allow_html=True,
        )

    st.markdown("---")
    st.subheader("Per-instance trade history")
    tab_labels = [f"{c['label']} (${c['equity']:,.0f})" for c in instances]
    tabs = st.tabs(tab_labels)

    for tab, card in zip(tabs, instances):
        with tab:
            iid = card["id"]
            hist = instance_trade_history(iid)
            ic1, ic2, ic3, ic4 = st.columns(4)
            ic1.metric("Bankroll", f"${card['bankroll']:,.0f}")
            ic2.metric("Equity", f"${card['equity']:,.2f}")
            ic3.metric("Win rate", f"{(card.get('win_rate') or 0):.1%}")
            ic4.metric("Trades", card["trades"])

            if hist:
                df = pd.DataFrame(hist)
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info(f"No trades yet for {card['label']}.")

    st.markdown("---")
    st.subheader("Market lane breakdown")
    st.caption("Slug-pattern view across the fleet (may overlap with instance scopes).")
    if scoped:
        lane_cols = st.columns(len(scoped))
        for col, lane in zip(lane_cols, scoped):
            with col:
                lp = lane["pnl"]
                wr = lane.get("wr")
                wr_txt = f"{wr:.1%}" if wr is not None else "—"
                st.markdown(f"**{lane['label']}**")
                st.markdown(f"Trades: {lane['n']} · WR: {wr_txt}")
                st.markdown(f"P&L: :{('green' if lp >= 0 else 'red')}[{fmt_pnl(lp)}]")

    st.markdown("---")
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Bandit state (per instance)")
        if bandits:
            for bid, bstate in bandits.items():
                with st.expander(f"Instance `{bid}`"):
                    st.json(bstate)
        else:
            st.info("No bandit state files yet.")

    with col_b:
        st.subheader("Strategy config")
        st.json(
            {
                "mode": cfg.get("mode", "moderate"),
                "min_edge": cfg.get("min_edge"),
                "min_conviction": cfg.get("min_conviction"),
                "extreme_q_high": cfg.get("extreme_q_high"),
                "extreme_q_low": cfg.get("extreme_q_low"),
                "kappa_base": cfg.get("kappa_base"),
                "max_single_market_pct": cfg.get("max_single_market_pct"),
                "per_instance_bankroll": PER_INSTANCE_BANKROLL,
                "fleet_bankroll": FLEET_BANKROLL,
            }
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    st.caption(f"Last loaded: {now} · refresh TTL {REFRESH_SEC}s")


if __name__ == "__main__":
    main()
