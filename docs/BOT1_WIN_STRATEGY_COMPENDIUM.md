# Bot-1 Win Strategy Compendium

_Consolidated quant research: daily income lanes, directional entry timing, and PRISM tier architecture._  
_Repo: https://github.com/minh99085/Bot-1 · PAPER ONLY until reconciliation + soak gates pass._

**Related implementation:** [PRISM_CURSOR_PHASES.txt](./PRISM_CURSOR_PHASES.txt) — 6-phase Cursor build prompts.

---

## Part 1 — Beyond Arb: How to Make Money Every Day

Dutch-book arb is real edge but **rare**: ~9 fills in 369K scans on the current epoch. It cannot fund steady daily income alone. The bot needs **volume × selectivity × size** on proven buckets.

### Reality check (daily PnL math)

| Target | What it takes |
|--------|----------------|
| $20–50/day | 8–15 trades at $15–25, **62%+ WR**, entry ~0.50 |
| $100+/day | ~20 trades/day OR $50+ size on proven buckets; capital $2K–5K + live execution |
| Arb-only | $0 most days; occasional +$30 dislocation — **not daily** |

### Five higher-frequency lanes (ranked)

#### 1. Sweet-spot selective directional (best daily candidate)

- **What:** Enter only when ask ∈ **$0.47–$0.55** (Osmani Discovery + `polymarket-asset-triage` skill).
- **Frequency:** 7 assets × 24 hourly windows → **5–15 trades/day** after filters.
- **Evidence (epoch ~2026-07-04):**
  - Selectivity counterfactual: WR 52.6% → **61.4%**, PnL +$2.79 → **+$32.78** (in-sample)
  - DOGE 1h: 66.7% WR, +$19.69
  - SOL 1h: 64.3% WR, +$18.49
  - BNB 1h: 30.8% WR, **−$27.76** → block
- **Conservative daily math:** 8 × $20 × 62% WR ≈ **+$16–25/day**

#### 2. Dep-arb as scalp (not hold)

- **What:** 94 actionable dep-arb violations vs 9 arb fills; problem is **hold-to-resolution** (−19.6% capture ratio).
- **Fix:** Conjunction-only, **mid-exit at 30s** when gap converges (100% convergence in lessons).
- **Frequency:** Multiple signals/hour on BTC 5m↔15m nesting.

#### 3. 5m/15m directional on 2–3 assets only

- **What:** Higher frequency than 1h; gate on `liquidity_danger` (61.3% accuracy), tight spread, `conviction=lean` (85.7%, n=14).
- **Pilot:** DOGE + SOL 5m only — not spray all assets.

#### 4. CEX-lead / stale-book sniper

- **What:** Enter when CEX moves and Polymarket ask lags (`stale_polymarket` divergence).
- **Avoid:** `stale_polymarket_down` (23.8% WR).

#### 5. Tail 10× band (< $0.10)

- **What:** `SKILL_ANALYSIS.md` + `automated_10x_arb.py`; rare lottery sleeve at $5 max.
- **Not steady** — one hit covers a week of small losses.

### What NOT to do (proven losers)

| Strategy | Why skip |
|----------|----------|
| Spray 7 assets on 1h | BNB alone −$27.76 |
| Hold dep-arb to resolution | −19.6% capture |
| LLM council as alpha | Grok shadow weak; Claude 8.4% approve |
| Market making | Historically −$255, removed |
| Scale before n≥200/bucket | n=78 directional is noise |

### Recommended daily-income stack

```
Arb (background lottery)     → rare +$30 hits, keep scanning
Sweet-spot 1h directional  → DOGE/SOL/ETH, 5–15 trades/day
Dep-arb 30s scalp           → 3–8 trades/day BTC 5m/15m
Tail 10× sleeve             → 0–2 bets/day, $5 max
```

**Priority:** (1) Kill BNB, sweet-spot only on DOGE/SOL/ETH, (2) Raise size to $15–25 on WR>60% buckets, (3) Dep-arb scalp mode, (4) Keep arb scanning.

---

## Part 2 — Sweet Timing for Directional Entry

The bot **partially** decides timing today — it blocks bad timing but does **not** hunt the optimal minute.

### How timing works today

| Layer | Role | Status |
|-------|------|--------|
| `min_seconds_since_open` | Hard floor **180s** (3 min) | ON |
| `LearnedHourlyEntryGate` | Blocks proven-losing intra-hour buckets (n≥20) | ON, immature |
| Baseline cohort TTC gate | Sweet 160–220s (8–11 min on 15m) | **OFF** for 1h |
| Mispricing / edge-TTC gates | Same sweet band | **OFF** |
| Osmani sweet-spot | Price $0.47–$0.55, not time | Parallel lane |

**Behavior:** Enter on first passing edge after 3 min — not at the best minute.

### Five learned intra-hour buckets

From `engine/pulse/hourly_entry_timing.py`:

| Bucket | Minutes after open | Settled data (epoch) |
|--------|-------------------|----------------------|
| `h0_5m` | 3–5 (floor blocks 0–3) | n=14, 50% WR, +$4.11 |
| **`h5_15m`** | **5–15** | n=21, 47.6% WR, **+$15.92** |
| `h15_30m` | 15–30 | n=3, 33% WR, **−$12.98** |
| `h30_45m` | 30–45 | n=9, 44% WR, +$3.76 |
| `h45_60m` | 45–60 | n=4, 50% WR, −$2.76 |

Finer 5-minute bands:

| Minutes | n | WR | PnL |
|---------|---|-----|------|
| 5–10m | 20 | 35% | +$14.36 |
| 15–20m | 3 | 33% | −$12.98 |
| **40–45m** | 7 | **57%** | **+$13.76** |
| 45–50m | 2 | 100% | +$7.24 |

### Sweet timing rules (from ledger)

| Priority | When | Why |
|----------|------|-----|
| **Must wait** | ≥ 180s (3 min) | 15m TV bar must exist |
| **Best window** | **300–720s (5–12 min)** | 15m RSI close + best PnL volume |
| **Secondary** | **2400–3000s (40–50 min)** | 45m bar + highest WR band |
| **Block** | **900–1800s (15–30 min)** | Worst bucket |
| **Price** | Ask **$0.47–$0.55** | Sweet-spot payoff (separate axis) |

### Per-asset timing (settled)

| Asset @ bucket | WR | PnL |
|----------------|-----|------|
| DOGE @ h5_15m | 100% (n=2) | +$19.21 |
| ETH @ h30_45m | 100% (n=2) | +$17.03 |
| XRP @ h5_15m | 60% (n=5) | +$14.71 |
| SOL @ h5_15m | 50% (n=8) | +$12.98 |
| BTC @ h5_15m | 0% (n=3) | −$2.46 |
| BNB @ all | toxic | −$27.76 total |

### Basic tiered TV + MC (stepping stone to PRISM)

Not “wait for all 4 TV bars” (that pushes to minute 55 and starves trades). Use:

1. **Tier 1 (required):** ≥ 300s + 15m TV fresh
2. **Tier 2 (preferred):** 30m agrees → full size
3. **Tier 3 (boost):** 45m+55m agree → size boost, not entry delay
4. **MC:** TV-adjusted drift → `mc_digital_p_up` → enter if EV > 3% (A) / 5% (S)

Env knobs: `PULSE_TV_MTF_REQUIRE_CONFIRM=1`, `PULSE_HOURLY_MIN_SECONDS_SINCE_OPEN=300`.

---

## Part 3 — PRISM: Posterior-Ranked Information State Machine

Full redesign beyond additive tier scores. **A tier is a live posterior**, not a label.

### Core thesis

> Each hourly window is a **sequential decision problem**. Information arrives over time; beliefs update every 15s tick; the bot chooses **enter / wait / skip** until the window expires.

### PRISM rank

```
R = I × max(0, E) × C

I = information completeness [0,1]   — signals arrived × freshness
E = ensemble MC edge vs ask        — after slippage
C = confidence [0,1]               — model agreement × bucket posterior
```

| Rank R | Agent | Size (illustrative) |
|--------|-------|-------------------|
| R ≥ 0.12 | **Sniper** | 10–20% of 35% slice, cap $200 |
| 0.06 ≤ R < 0.12 | **Strike** | 4–8% |
| 0.03 ≤ R < 0.06 | **Harvester** | 1–2%, cap $25 |
| R < 0.03 | Wait / skip | $0 |

### Layer 1 — Information arrival I(t)

| Signal | Arrives ~ | Half-life | Weight |
|--------|-----------|-----------|--------|
| Chainlink open | 0s | ∞ | 0.05 |
| CEX lead | continuous | 45s | 0.20 |
| Book imbalance | continuous | 30s | 0.15 |
| TV 15m | 15m | 12m | 0.18 |
| TV 30m | 30m | 25m | 0.15 |
| TV 45m | 45m | 35m | 0.12 |
| TV 55m | 55m | 45m | 0.10 |
| Quant digital fair | continuous | 60s | 0.15 |

```
I(t) = Σ wᵢ × freshnessᵢ(t) × 𝟙[observed]
```

- t=3m → I ≈ 0.15 (no Sniper; I_floor=0.70)
- t=15m + 15m TV → I ≈ 0.55
- t=32m + 15m+30m aligned → I ≈ 0.78 (Sniper eligible)

**Timing FSM:** WATCHING → TIER1_READY (3–12m) → TIER2_CONFIRM (12–35m) → LATE_WINDOW (35–50m) → EXPIRED (>50m).

### Layer 2 — Bayesian belief (not point scoring)

```python
logit_posterior = logit(ask_prior) + Σ LRᵢ(signalᵢ) × freshnessᵢ(t)
posterior_p = sigmoid(logit_posterior)
```

Likelihood ratios recalibrated nightly from settled trades. TV conflict → LR 0.55; 15m+30m agree → 1.35; `stale_polymarket_down` → 0.40.

### Layer 3 — MC ensemble (4 models)

| Model | Role |
|-------|------|
| M1 | Closed-form digital (baseline) |
| M2 | TV-drift GBM MC |
| M3 | Jump-diffusion (`liquidity_danger`) |
| M4 | Regime-switching HMM |

```
E = weighted_mean(EV₁..₄)
C_mc = 1 - normalized_std(p_up₁..₄)
```

Low agreement → Probe only, never Sniper.

### Layer 4 — Optimal stopping

Each tick:

```
V_enter = E × C × payoff_shape(ask)
V_wait  = discount × E[future R] - opportunity_cost(locked capital)

ENTER if R ≥ R_min AND V_enter > V_wait
WAIT  if I < I_target and t < 50m
SKIP  if t > 50m OR R falling 3 ticks OR E < 0
```

**Conviction velocity:** v_E = dE/dt — enter when edge rising; skip when decaying.

### Layer 5 — Thompson sampling (no hardcoded whitelist)

Cell = `(asset, minute_band, regime, tv_pattern)` → Beta(α, β).  
Size from Thompson draw; toxic cells auto-block; BNB seeded pessimistic until proven.

### Layer 6 — Three agents + capital split

| Slice | % | Role |
|-------|---|------|
| Arb reserve | 40% | Dutch-book — untouched |
| PRISM Sniper | 35% | R ≥ 0.12, 2–4 trades/day, big size |
| Harvester | 15% | 0.03 ≤ R < 0.06, stale-book micro-edges |
| Buffer | 10% | Drawdown halt |

**Daily loss halt:** −12% of agent slice → 6h pause.

### Layer 7 — Adversarial book intelligence

| Signal | Effect |
|--------|--------|
| CEX +30bps, ask +3bps | Sniper boost (stale) |
| CEX +30bps, ask +20bps | Skip (arb'd away) |
| Spread 2× in 60s | C × 0.7 |
| Depth −50% | Size × 0.5 |

### Layer 8 — Cross-asset lead-lag

```
BTC (0s) → ETH (5s) → SOL (15s) → DOGE (30s) → XRP (45s)
```

Transfer posterior to laggard assets before belief update.

### Size formula

```python
raw = bankroll × agent_slice × tanh(R / 0.08) × C × thompson_mult
raw *= half_kelly(p_win, ask)
raw = min(raw, depth × 0.25, agent_cap)
raw *= (1 - 0.3 × open_correlation)
```

### Expected daily PnL (mature paper, $2K bankroll)

| Agent | Trades | Avg size | WR | Daily EV |
|-------|--------|----------|-----|----------|
| Arb tail | ~0.02/day | $400 | ~100% | +$5 |
| Sniper | 3 | $120 | 66% | **+$47** |
| Harvester | 10 | $18 | 58% | **+$14** |
| **Total** | | | | **~$66/day** |

Scale ~linearly with bankroll; haircut ~30% for live slippage.

### PRISM vs basic tiers

| Basic tiers | PRISM |
|-------------|-------|
| Add points → label | Bayesian posterior every tick |
| First pass enters | Optimal stopping |
| Static asset matrix | Thompson discovers cells |
| TV as bonus | TV as decaying likelihood |
| Single MC | 4-model ensemble + disagreement |
| One directional lane | Sniper + Harvester + Arb |
| Ignore cross-asset | Lead-lag propagation |

### Implementation map

| Phase | Module | Status |
|-------|--------|--------|
| 1 | `prism/belief.py` | See [PRISM_CURSOR_PHASES.txt](./PRISM_CURSOR_PHASES.txt) |
| 2 | `prism/information.py` | |
| 3 | `prism/stopping.py` | |
| 4 | `prism/ensemble_mc.py` | |
| 5 | `prism/thompson.py` | |
| 6 | `prism/agents.py` + `cross_asset.py` + engine wire-up | |

### Safety (never lifted)

- `execution_gate.py` sole fill authority
- Paper-only until `global_reconciled=true`
- Arb reserve not used for directional
- Max 2 correlated 1h positions
- BNB blocked until bucket proves otherwise

---

## Execution roadmap (operator)

1. **Now:** Run Cursor phases 1→6 from [PRISM_CURSOR_PHASES.txt](./PRISM_CURSOR_PHASES.txt) (one phase at a time).
2. **After Phase 6:** 48h paper soak → pull `vps_full_reports/latest/`.
3. **Go/no-go:** Sniper WR > 60%, n ≥ 15, daily PnL > $30 paper on $500 (scale test on $2K).
4. **Live:** Only after reconciliation clean + 500+ settled Sniper/Harvester trades.

---

## One-line summary

**Daily money** = sweet-spot directional on DOGE/SOL/ETH + dep-arb scalps + arb lottery.  
**Sweet timing** = 5–12 min primary, 40–50 min secondary, block 15–30 min.  
**Big money** = PRISM Sniper sizes by **how much the bot knows** (I × E × C), not flat $5.

---

_Document version: 2026-07-06 · Sources: Bot-1 VPS reports epoch 2026-07-04-fresh-trading, engine code, quant session analysis._