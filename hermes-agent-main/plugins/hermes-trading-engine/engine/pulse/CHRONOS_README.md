# CHRONOS — Pre-Decision Validation Engine

**Chronological Holdout Replay Operating Numerical Scorecard**

Invented quant method: unified pre-decision testing before bet size and trade authority.

## Why this exists

Bot-3 runs **15+ mathematical modules** with no shared pre-flight test:

| Module | Math | Pre-decision test before ship |
|--------|------|-------------------------------|
| execution_gate | VWAP ladder, EV = p − fill − fee | Unit tests only |
| fair_value | Φ(d₂) digital GBM | Sign/bounds tests |
| tier_engine | Log-odds Bayes, Kelly, z-displacement | Tier assignment tests |
| selectivity | Wilson UB, BH-FDR, breakeven WR | **In-sample** counterfactual only |
| gate_auto_tune | Rolling WR, step clamps | Post-settlement synthetic |
| SAWR | Pareto utility, Beta affinity | Unit tests; 12-trade replay inconclusive |
| binary_intel | z, θ, entropy, Kelly haircut | Formula tests |
| pre_trade_analysis | Weighted score, Wilson buckets | Component tests |
| lane_15m / cross_horizon | Wilson LB policy rewrite | Synthetic buckets |
| p_exec | Brier blend, context promote | Unit tests |
| edge_model | Logistic SGD, ECE | Model tests |
| sizing | Half-Kelly × readiness | Kelly arithmetic tests |
| prism/thompson | Beta posteriors, Thompson draw | Math tests |
| walk_forward.py | Holdout PF | **No unit tests; not wired pre-trade** |

**Gap:** No chronological walk-forward replay before sizing or policy changes.

## CHRONOS three layers

### Layer A — Trade Certificate (every Osmani fill)

Before `decide_trade_size()`:

1. Build **context key**: `asset|lane|side|price_bucket|ttc_bucket`
2. Cohort = settled trades with same context and `entry_ts < now` (strict chronology)
3. Compute:
   - `w_lb` = Wilson lower bound on cohort WR
   - `breakeven` = ask (binary EV = p − ask)
   - **CVS** = `(w_lb − ask) + (n/(n+4))·log(1+n) − 1.5·max(0, ask − w_lb)`
   - **Kelly dry-run** = `max(0, (w_lb − ask)/(1−ask)) × kelly_fraction`

4. **Verdict:**
   - `proceed` — CVS ≥ proceed_cvs and w_lb ≥ breakeven → full Kelly cap
   - `probe` / `cold_probe` — thin history or marginal CVS → base size only
   - `block` — w_lb < breakeven − margin (exploration carve-out)

### Layer B — Policy Veto (gate tighten/loosen)

Before `GateAutoTuner` or `SAWR` applies `loosen`:

1. Split ledger by `entry_ts`: train 70% / holdout 30%
2. Compute holdout Wilson LB WR
3. **Veto loosen** if holdout w_lb < kill_wr

Before blocking a losing context (future): walk-forward block replay must improve holdout PF.

### Layer C — Size cap

`size_cap_mult` from Kelly dry-run — never upsize without conservative edge proof.

## Env

```
PULSE_CHRONOS_ENABLED=1
PULSE_CHRONOS_MIN_COHORT_N=4
PULSE_CHRONOS_PROCEED_CVS=0.05
PULSE_CHRONOS_EXPLORATION_RATE=0.12
PULSE_CHRONOS_KILL_WR=0.48
```

## Integration

- `engine.py` Osmani path — before `decide_trade_size`
- `gate_auto_tune.py` — veto loosen
- `sawr_controller.py` — veto loosen
- Dashboard — `chronos` status block
- Ledger persist — `accounting_state.chronos`

## What CHRONOS does NOT do

- Replace `execution_gate` (live orderbook VWAP)
- Backtest on external historical candles
- Force trades (restrict-only + probe sizing)

## File

`engine/pulse/chronos_validator.py`
