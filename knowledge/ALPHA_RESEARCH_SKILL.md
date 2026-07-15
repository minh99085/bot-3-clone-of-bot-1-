# ALPHA_RESEARCH_SKILL.md

> Alpha + **allocation** living skill. Read by `signal_generator`, `portfolio`,
> and `verifier` every turn. Grows via `lessons_engine` promotions from LESSONS.md.

## Objective

Produce sparse, high-EV signals **and** allocate capital across sub-strategies
so the book hits **consistent 80%+ WR** with DD &lt; 8% and PF &gt; 1.4.

Every unique `(market_series | entry_mode | regime | hourly_bucket)` is a
**sub-strategy / return source**. Allocation is first-class — not an afterthought.

## Regime Filters

Trade only when regime ∈ {`mean_revert`, `trending_up`, `trending_down`, `low_vol`}.
Reject `unknown`. Be defensive in `high_vol` (require tier A + EV ≥ 0.08).

## Confidence Tiers

| Tier | Conviction | Verifier |
|------|------------|----------|
| A | ≥ 0.75 | Eligible |
| B | ≥ 0.55 | Eligible |
| C / D | &lt; 0.55 | **REJECT** |

## Entry Modes

| Mode | Status | Notes |
|------|--------|-------|
| `mean_reversion` | ACTIVE | Preferred with DOWN bias mid-day |
| `momentum` | ACTIVE | Require clean trend regime |
| `liquidity_sweep` | ACTIVE | Tight spread + stability |
| `news_shock` / `grok_signal` / `tv_signal` | PAPER_ONLY | Hard verify |
| `osmani_lane` | **GATED** | CUT when degrading; never raw-cov size |

## Portfolio Construction (Ruuj layer)

### Robust base

1. Build sub-strategy return matrix from settlements
2. **Ledoit-Wolf shrink** covariance — never raw sample cov
3. Base weights = **HRP** (n≥2, T≥8) else **edge-weighted risk parity**
4. Cap single sleeve ≤ 25%; gross new risk ≤ 35% of capital

### Black-Litterman views

Blend prior with views from:

- Grok conviction (`meta.grok_conviction`)
- TV alignment (`meta.tv_alignment`)
- Live EV + tier + conviction

Low-confidence views barely move weights; high-confidence tilts meaningfully (`tau=0.05`).

### Dynamic sizing in Handoff

`allocation_handoff()` sizes each signal by:

- Sleeve HRP/BL weight
- Edge share within sleeve
- Diversification contribution
- Cut/reduce weight caps

### Verifier allocation gates

PASS requires **signal AND allocation**:

- Sub-strategy not on CUT list
- Non-zero approved size
- HHI ≤ 0.45 (reject oversized adds into concentrated books)
- Diversification ratio not collapsing below 1.05 on large adds

## Cut / Reduce Logic (Chapter 5)

Track per-sleeve **internal confidence** from:

- Rolling EV after cost
- WR + WR trend
- EV trend
- Regime stability
- Brier score

| Condition | Action | Meaning |
|-----------|--------|---------|
| Model broken (EV&lt;0.02, WR trend broken, brier bad, regime unstable, osmani degrading) | **CUT** | Reason-for-working broken — weight=0 even if still +PnL |
| Degrading confidence / currently_losing + neg EV trend | **REDUCE** | Cap ≤ 8% — temporary pain ≠ model death |
| Rising confidence + diversifying | **BOOST** | Rare; still capped |

**Never confuse currently_losing with model_broken.**

## Edge Buckets — EXPLOIT

| Regime | Hour | Mode | n | WR | Edge | PF |
|--------|------|------|---|----|------|----|
| mean_revert | 14 | mean_reversion | 48 | 78% | 0.09 | 1.90 |
| trending_down | 20 | momentum | 35 | 71% | 0.07 | 1.55 |

## Edge Buckets — AVOID

| Mode / Regime | Reason |
|---------------|--------|
| osmani_lane | Gated / CUT when degrading |
| unknown regime | No filter pass |
| LESSONS `AVOID:` / `CUT:` | Binding |

## Allocation Heuristics (seed)

- REDUCE weight on `osmani_lane` when rolling EV &lt; threshold or in toxic hour buckets
- Prefer diversifying `btc_updown` vs `eth_updown` sleeves when corr high under LW cov
- After sleeve loss: REDUCE first; CUT only if confidence metrics say model broken
- Do not chase concentration — HHI gate is hard

## DOWN Bias (explicit + dynamic)

Base from STATE `Down Bias` (0.35). Adjust by regime (see SKILL.md).

## Execution Quality

- `entry_vwap_target` inside spread; `pre_entry_stability_ok` required
- Fees 100 bps + slippage 40 bps in live EV

## Daily / Rolling Gates

Pause if WR(20)&lt;55%, PF(20)&lt;1.2, daily loss&gt;3%, DD≥8%, consec losses≥4.

## Auto-Promoted Allocation Rules

<!-- lessons_engine appends CUT/REDUCE/ALLOCATION rules here -->

## Auto-Promoted Rules

<!-- lessons_engine appends signal rules here -->
