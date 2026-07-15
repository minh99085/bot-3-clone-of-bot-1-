"""Portfolio construction — Ledoit-Wolf + HRP / edge-weighted RP + Black-Litterman.

Ruuj-inspired robust allocation:
  1. Never use raw sample covariance → Ledoit-Wolf shrinkage
  2. Base weights via Hierarchical Risk Parity (or edge-weighted RP when n small)
  3. Tilt with Black-Litterman-style views (Grok / TV / verifier conviction)
  4. Apply per-sub-strategy weight caps from cut/reduce confidence layer

Wired into Handoff: sizes verified opportunities before executor runs.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from hermes.models import (
    AllocationProposal,
    PortfolioSnapshot,
    Signal,
    SubStrategyAction,
    SubStrategyConfidence,
)
from hermes.state_io import (
    append_jsonl,
    ensure_dirs,
    ledger_path,
    parse_state_fields,
    read_jsonl,
    read_state_md,
    update_state_field,
    write_handoff,
)
from hermes.substrategy import (
    annotate_signal,
    composite_confidence,
    decide_action,
    default_confidence,
    make_substrategy_id,
)

logger = logging.getLogger(__name__)

MAX_SINGLE_WEIGHT = 0.25
MIN_TICKET_USD = 15.0
MAX_GROSS_EXPOSURE = 0.35  # of capital across open + new
TAU = 0.05  # BL uncertainty scalar


# ── Covariance: Ledoit-Wolf shrinkage (never raw sample) ─────────────────────


def ledoit_wolf_shrink(returns: np.ndarray) -> tuple[np.ndarray, float]:
    """Ledoit-Wolf covariance shrinkage toward scaled identity.

    returns: shape (T, N) demeaned or raw; columns = sub-strategies.
    Returns (cov, shrinkage intensity).
    """
    x = np.asarray(returns, dtype=float)
    if x.ndim != 2 or x.shape[0] < 2 or x.shape[1] < 1:
        n = max(1, x.shape[1] if x.ndim == 2 else 1)
        return np.eye(n) * 0.01, 1.0

    t, n = x.shape
    x = x - x.mean(axis=0, keepdims=True)
    sample = (x.T @ x) / t

    # Target: mu * I where mu = average variance
    mu = float(np.trace(sample) / n)
    target = mu * np.eye(n)

    # Shrinkage intensity (Ledoit-Wolf 2004 simplified)
    x2 = x ** 2
    p_hat = float(np.sum((x2.T @ x2) / t - sample ** 2))
    # Avoid div0
    d_hat = float(np.sum((sample - target) ** 2))
    kappa = p_hat / d_hat if d_hat > 1e-12 else 1.0
    shrinkage = max(0.0, min(1.0, kappa / t))
    cov = shrinkage * target + (1.0 - shrinkage) * sample
    # Ensure PSD numerically
    eigvals, eigvecs = np.linalg.eigh(cov)
    eigvals = np.clip(eigvals, 1e-10, None)
    cov = eigvecs @ np.diag(eigvals) @ eigvecs.T
    return cov, float(shrinkage)


# ── Hierarchical Risk Parity ─────────────────────────────────────────────────


def _corr_from_cov(cov: np.ndarray) -> np.ndarray:
    d = np.sqrt(np.clip(np.diag(cov), 1e-12, None))
    inv = 1.0 / d
    corr = cov * np.outer(inv, inv)
    np.fill_diagonal(corr, 1.0)
    return np.clip(corr, -1.0, 1.0)


def _distance(corr: np.ndarray) -> np.ndarray:
    return np.sqrt(np.clip(0.5 * (1.0 - corr), 0.0, None))


def _seriation(dist: np.ndarray) -> list[int]:
    """Simple single-linkage seriation (Lopez de Prado HRP quasi-diag)."""
    n = dist.shape[0]
    if n == 1:
        return [0]
    # Agglomerative clustering via scipy if available; else greedy
    try:
        from scipy.cluster.hierarchy import linkage, leaves_list
        from scipy.spatial.distance import squareform

        condensed = squareform(dist, checks=False)
        link = linkage(condensed, method="single")
        return list(leaves_list(link))
    except Exception:  # noqa: BLE001
        order = [0]
        remaining = set(range(1, n))
        while remaining:
            last = order[-1]
            nxt = min(remaining, key=lambda j: dist[last, j])
            order.append(nxt)
            remaining.remove(nxt)
        return order


def _cluster_var(cov: np.ndarray, items: list[int]) -> float:
    sub = cov[np.ix_(items, items)]
    w = np.ones(len(items)) / len(items)
    return float(w @ sub @ w)


def hrp_weights(cov: np.ndarray) -> np.ndarray:
    """Hierarchical Risk Parity weights from shrunk covariance."""
    n = cov.shape[0]
    if n == 1:
        return np.array([1.0])
    corr = _corr_from_cov(cov)
    dist = _distance(corr)
    order = _seriation(dist)
    w = np.ones(n)

    def _recurse(items: list[int]) -> None:
        if len(items) <= 1:
            return
        split = len(items) // 2
        left, right = items[:split], items[split:]
        var_l = _cluster_var(cov, left)
        var_r = _cluster_var(cov, right)
        alpha = 1.0 - var_l / (var_l + var_r + 1e-12)
        for i in left:
            w[i] *= alpha
        for i in right:
            w[i] *= 1.0 - alpha
        _recurse(left)
        _recurse(right)

    _recurse(order)
    w = np.clip(w, 0.0, None)
    s = w.sum()
    return w / s if s > 0 else np.ones(n) / n


def edge_weighted_risk_parity(
    vols: np.ndarray,
    edges: np.ndarray,
) -> np.ndarray:
    """Fallback when T is tiny: inverse-vol × edge quality, renormalized."""
    inv = 1.0 / np.clip(vols, 1e-6, None)
    score = inv * np.clip(edges, 0.0, None)
    if score.sum() <= 0:
        score = inv
    return score / score.sum()


# ── Black-Litterman-style views ──────────────────────────────────────────────


def black_litterman_tilt(
    prior_w: np.ndarray,
    cov: np.ndarray,
    view_returns: np.ndarray,
    view_conf: np.ndarray,
    *,
    tau: float = TAU,
) -> np.ndarray:
    """Blend risk-based prior weights with absolute views.

    Low-confidence views barely move weights; high-confidence tilts meaningfully.
    view_returns: expected excess return proxy per sleeve
    view_conf: [0,1] confidence → Ω diagonal
    """
    n = len(prior_w)
    if n == 0:
        return prior_w
    # Equilibrium returns π from prior (reverse optimization)
    # risk aversion δ ≈ 2.5 typical; scale by vol
    port_var = float(prior_w @ cov @ prior_w) + 1e-12
    delta = 2.5
    pi = delta * cov @ prior_w

    conf = np.clip(view_conf, 0.01, 0.99)
    # Ω: lower conf → larger uncertainty
    omega_diag = (1.0 - conf) / conf * np.diag(tau * cov)
    omega_diag = np.clip(omega_diag, 1e-8, None)
    omega_inv = np.diag(1.0 / omega_diag)

    p = np.eye(n)  # absolute views on each sleeve
    tau_cov_inv = np.linalg.pinv(tau * cov)
    mid = tau_cov_inv + p.T @ omega_inv @ p
    rhs = tau_cov_inv @ pi + p.T @ omega_inv @ view_returns
    mu_bar = np.linalg.solve(mid, rhs)

    # Mean-variance with BL posterior (active tilt from prior)
    raw = np.linalg.pinv(delta * cov) @ mu_bar
    raw = np.clip(raw, 0.0, None)
    if raw.sum() <= 0:
        return prior_w
    bl_w = raw / raw.sum()
    # Confidence-weighted blend: high avg conf → more BL, else stay near prior
    avg_c = float(conf.mean())
    blend = 0.25 + 0.75 * avg_c
    out = (1.0 - blend) * prior_w + blend * bl_w
    out = np.clip(out, 0.0, None)
    return out / out.sum()


def view_from_signal(signal: Signal) -> tuple[float, float]:
    """Map Grok/TV/verifier-style conviction into (view_return, confidence)."""
    # Expected return proxy ≈ live_ev
    view_r = float(signal.live_ev)
    # Confidence from conviction + tier + optional meta views
    base = float(signal.conviction)
    tier_boost = {"A": 0.15, "B": 0.05, "C": -0.1, "D": -0.2}.get(
        signal.confidence_tier.value, 0.0
    )
    grok = float(signal.meta.get("grok_conviction", 0.0) or 0.0)
    tv = float(signal.meta.get("tv_alignment", 0.0) or 0.0)
    verifier_hint = float(signal.meta.get("verifier_score_hint", 0.0) or 0.0)
    conf = max(
        0.05,
        min(
            0.95,
            0.45 * base
            + tier_boost
            + 0.25 * grok
            + 0.15 * tv
            + 0.15 * verifier_hint,
        ),
    )
    return view_r, conf


# ── Returns matrix from ledger ───────────────────────────────────────────────


def _settlement_rows(paper: bool = True) -> list[dict]:
    rows = read_jsonl(ledger_path(paper=paper))
    out = []
    for r in rows:
        if r.get("event") == "settlement" or r.get("won") is not None:
            out.append(r)
    return out


def build_returns_matrix(
    substrategy_ids: list[str],
    paper: bool = True,
    lookback: int = 60,
) -> tuple[np.ndarray, dict[str, SubStrategyConfidence]]:
    """Build (T, N) return matrix of settled PnL% per sleeve + confidence map."""
    settles = _settlement_rows(paper=paper)[-lookback * 3 :]
    # Group pnl by substrategy chronologically
    series: dict[str, list[float]] = {s: [] for s in substrategy_ids}
    meta: dict[str, list[dict]] = {s: [] for s in substrategy_ids}

    for r in settles:
        sid = r.get("substrategy_id") or ""
        if not sid:
            # Reconstruct from fields if present
            mode = r.get("entry_mode", "mean_reversion")
            regime = r.get("regime", "unknown")
            hour = r.get("hourly_bucket", 0)
            series_name = r.get("market_series", "misc")
            if isinstance(mode, dict):
                mode = mode.get("value", "mean_reversion")
            if isinstance(regime, dict):
                regime = regime.get("value", "unknown")
            sid = make_substrategy_id(series_name, mode, regime, int(hour or 0))
        if sid not in series:
            continue
        size = float(r.get("size_usd", 0) or 1.0) or 1.0
        pnl = float(r.get("pnl_usd", 0) or 0.0)
        series[sid].append(pnl / size)
        meta[sid].append(r)

    # Pad to rectangular: use zeros for missing (conservative)
    lengths = [len(v) for v in series.values()]
    t = max(lengths) if lengths else 0
    t = min(t, lookback) if t else 0
    n = len(substrategy_ids)
    if t < 2:
        mat = np.zeros((max(2, t), n))
    else:
        mat = np.zeros((t, n))
        for j, sid in enumerate(substrategy_ids):
            vals = series[sid][-t:]
            mat[-len(vals) :, j] = vals

    confidences: dict[str, SubStrategyConfidence] = {}
    for sid in substrategy_ids:
        rows = meta.get(sid, [])
        if not rows:
            confidences[sid] = default_confidence(sid)
            continue
        pnls = [float(r.get("pnl_usd", 0) or 0) for r in rows]
        sizes = [float(r.get("size_usd", 1) or 1) for r in rows]
        rets = [p / (s or 1) for p, s in zip(pnls, sizes)]
        wins = [1 if (r.get("won") or float(r.get("pnl_usd", 0)) > 0) else 0 for r in rows]
        n_s = len(rets)
        half = max(1, n_s // 2)
        wr = sum(wins) / n_s
        wr_recent = sum(wins[-half:]) / half
        wr_old = sum(wins[:half]) / half if n_s >= 2 else wr
        ev = float(np.mean(rets)) if rets else 0.0
        ev_recent = float(np.mean(rets[-half:])) if rets else 0.0
        ev_old = float(np.mean(rets[:half])) if n_s >= 2 else ev
        # Simple brier: (p_win_pred - outcome)^2 avg; use rolling_wr as pred
        brier = float(np.mean([(wr - o) ** 2 for o in wins])) if wins else 0.25
        currently_losing = len(wins) >= 3 and sum(wins[-3:]) == 0
        c = default_confidence(sid)
        c.sample_n = n_s
        c.rolling_ev = ev
        c.rolling_wr = wr
        c.wr_trend = wr_recent - wr_old
        c.ev_trend = ev_recent - ev_old
        c.brier_score = brier
        c.currently_losing = currently_losing
        c.regime_stability = 0.85 if n_s >= 10 else 0.6
        c.internal_confidence = composite_confidence(
            rolling_ev=c.rolling_ev,
            rolling_wr=c.rolling_wr,
            wr_trend=c.wr_trend,
            ev_trend=c.ev_trend,
            regime_stability=c.regime_stability,
            brier_score=c.brier_score,
        )
        confidences[sid] = decide_action(c)
    return mat, confidences


# ── Core allocator ───────────────────────────────────────────────────────────


def allocate(
    signals: list[Signal],
    *,
    capital_usd: float,
    open_exposure_usd: float = 0.0,
    paper: bool = True,
    confidences: Optional[dict[str, SubStrategyConfidence]] = None,
) -> tuple[AllocationProposal, list[Signal]]:
    """Produce allocation proposal and annotate signals with sizes/weights.

    Handoff step: dynamic sizing by edge quality, diversification, sleeve health.
    """
    ensure_dirs()
    if not signals:
        return (
            AllocationProposal(capital_usd=capital_usd, notes="no signals"),
            [],
        )

    annotated: list[Signal] = [annotate_signal(s.model_copy(deep=True)) for s in signals]
    ids = []
    for s in annotated:
        if s.substrategy_id not in ids:
            ids.append(s.substrategy_id)

    rets, tracked = build_returns_matrix(ids, paper=paper)
    if confidences:
        tracked.update(confidences)

    cov, shrink = ledoit_wolf_shrink(rets)
    vols = np.sqrt(np.clip(np.diag(cov), 1e-12, None))
    edges = np.array(
        [
            max(
                tracked.get(sid, default_confidence(sid)).rolling_ev,
                next((s.live_ev for s in annotated if s.substrategy_id == sid), 0.0),
            )
            for sid in ids
        ]
    )

    method = "hrp_edge_bl"
    if rets.shape[0] >= 8 and len(ids) >= 2:
        prior = hrp_weights(cov)
        method = "hrp_edge_bl"
    else:
        prior = edge_weighted_risk_parity(vols, edges)
        method = "edge_rp_bl"

    # Views from signals (aggregate per sleeve: max conviction view)
    view_r = np.zeros(len(ids))
    view_c = np.zeros(len(ids))
    for j, sid in enumerate(ids):
        sleeve_sigs = [s for s in annotated if s.substrategy_id == sid]
        best = max(sleeve_sigs, key=lambda s: s.conviction)
        vr, vc = view_from_signal(best)
        view_r[j] = vr
        view_c[j] = vc
        best.view_confidence = vc

    weights = black_litterman_tilt(prior, cov, view_r, view_c)

    # Apply cut/reduce caps
    cut_list: list[str] = []
    reduce_list: list[str] = []
    caps = np.ones(len(ids)) * MAX_SINGLE_WEIGHT
    for j, sid in enumerate(ids):
        conf = tracked.get(sid) or default_confidence(sid)
        if conf.action == SubStrategyAction.CUT:
            caps[j] = 0.0
            cut_list.append(sid)
        elif conf.action == SubStrategyAction.REDUCE:
            caps[j] = min(caps[j], conf.weight_cap, 0.08)
            reduce_list.append(sid)
        else:
            caps[j] = min(caps[j], conf.weight_cap)

    weights = np.minimum(weights, caps)
    if weights.sum() <= 1e-12:
        # Everything cut — zero allocation
        weights = np.zeros(len(ids))
    else:
        weights = weights / weights.sum()
        # Re-cap after renormalize (iterative once)
        weights = np.minimum(weights, caps)
        if weights.sum() > 0:
            weights = weights / weights.sum()

    # Diversification ratio: sum(w*σ) / σ_port
    port_vol = float(np.sqrt(weights @ cov @ weights + 1e-12))
    div_ratio = float((weights @ vols) / port_vol) if port_vol > 0 else 1.0
    hhi = float(np.sum(weights ** 2))

    # Budget for new risk
    room = max(0.0, capital_usd * MAX_GROSS_EXPOSURE - open_exposure_usd)
    weight_map = {sid: float(w) for sid, w in zip(ids, weights)}
    size_map: dict[str, float] = {}

    # Split sleeve budget across signals in that sleeve (edge-weighted)
    for sid, w in weight_map.items():
        sleeve_budget = room * w
        sleeve_sigs = [s for s in annotated if s.substrategy_id == sid]
        if not sleeve_sigs or sleeve_budget <= 0:
            for s in sleeve_sigs:
                s.allocation_weight = 0.0
                s.allocation_usd = 0.0
                s.diversification_contrib = 0.0
                size_map[s.signal_id] = 0.0
            continue
        edge_sum = sum(max(0.0, s.live_ev) for s in sleeve_sigs) or 1.0
        for s in sleeve_sigs:
            share = max(0.0, s.live_ev) / edge_sum
            # Also respect original suggested size as soft cap
            usd = min(sleeve_budget * share, s.size_usd_suggested * 1.5, capital_usd * MAX_SINGLE_WEIGHT)
            if usd < MIN_TICKET_USD:
                usd = 0.0
            s.allocation_weight = w * share
            s.allocation_usd = round(usd, 2)
            s.diversification_contrib = div_ratio * share
            s.size_usd_suggested = s.allocation_usd or s.size_usd_suggested
            size_map[s.signal_id] = s.allocation_usd

    proposal = AllocationProposal(
        method=method,
        capital_usd=capital_usd,
        weights=weight_map,
        signal_sizes_usd=size_map,
        diversification_ratio=round(div_ratio, 4),
        portfolio_vol_proxy=round(port_vol, 6),
        concentration_hhi=round(hhi, 4),
        cut_list=cut_list,
        reduce_list=reduce_list,
        view_tilts={sid: float(c) for sid, c in zip(ids, view_c)},
        notes=f"shrinkage={shrink:.3f} room=${room:.2f}",
    )
    logger.info(
        "allocate: method=%s n=%d div=%.2f hhi=%.3f cut=%d reduce=%d",
        method,
        len(ids),
        div_ratio,
        hhi,
        len(cut_list),
        len(reduce_list),
    )
    return proposal, annotated


def persist_portfolio_state(proposal: AllocationProposal, paper: bool = True) -> PortfolioSnapshot:
    """Write portfolio metrics into STATE.md + ledger."""
    snap = PortfolioSnapshot(
        capital_usd=proposal.capital_usd,
        n_substrategies_active=sum(1 for w in proposal.weights.values() if w > 1e-6),
        n_cut=len(proposal.cut_list),
        n_reduce=len(proposal.reduce_list),
        diversification_ratio=proposal.diversification_ratio,
        concentration_hhi=proposal.concentration_hhi,
        open_exposure_usd=sum(proposal.signal_sizes_usd.values()),
        top_weights=dict(
            sorted(proposal.weights.items(), key=lambda kv: -kv[1])[:5]
        ),
    )
    update_state_field("Diversification Ratio", f"{snap.diversification_ratio:.3f}")
    update_state_field("Concentration HHI", f"{snap.concentration_hhi:.3f}")
    update_state_field("Substrategies Active", str(snap.n_substrategies_active))
    update_state_field("Substrategies Cut", str(snap.n_cut))
    update_state_field("Substrategies Reduce", str(snap.n_reduce))
    update_state_field("Allocation Method", proposal.method)
    append_jsonl(ledger_path(paper=paper).parent / "portfolio_snapshots.jsonl", snap)
    return snap


def allocation_handoff(
    signals: list[Signal],
    turn_id: str,
    paper: bool = True,
) -> tuple[AllocationProposal, list[Signal]]:
    """Public Handoff entry: size signals, persist proposal."""
    state = parse_state_fields(read_state_md())
    capital = float(state.get("capital_usd", state.get("capital", 10_000)) or 10_000)
    open_exp = float(state.get("open_exposure_usd", 0) or 0)
    proposal, sized = allocate(
        signals, capital_usd=capital, open_exposure_usd=open_exp, paper=paper
    )
    write_handoff("allocation", [proposal], turn_id)
    write_handoff("signals_sized", sized, turn_id)
    persist_portfolio_state(proposal, paper=paper)
    return proposal, sized
