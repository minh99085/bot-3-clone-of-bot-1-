# SAWR — Self-Adjusting Win-Rate Controller

Invented quant method for Bot-3 (PAPER ONLY). Meta-layer over existing learners.

## Problem

GateAutoTuner, Lane15m, CrossHorizon, Selectivity, and Binary Intel each nudge
overlapping knobs without a shared objective. They can fight (one loosens while
another tightens) and there is no explicit **WR × fill-rate** utility.

## Invention

### 1. Fill-Quality Pareto utility

```
U = w_wr · Wilson_LB(WR, n) + w_fill · log(1 + fills/h)
    − λ · max(0, kill_wr − WR)
```

- **Wilson LB** = conservative win-rate (punishes small-n lucky streaks).
- **log fills** = throughput term so the bot does not starve learning.
- **kill penalty** = hard cost for WR below kill floor.

### 2. Empirical-Bayes side affinity

Per `(asset, lane, side)` maintain `Beta(α, β)` with weakly informative prior
(~55% WR). After each settlement, update α/β. At pre-trade:

- `edge = E[p] − ask`
- Positive edge → size boost (≤ 1.25×)
- Negative edge with enough samples → soft block / size cut

### 3. Adaptive step shrinkage

```
η_t = η₀ / (1 + √n_adjustments) · regime_factor
```

`regime_factor` shrinks when rolling model Brier exceeds market-mid Brier
(calibration regime shift).

### 4. Conflict arbitration

If WR < kill floor → `stance=veto_loosen`. GateAutoTuner skips loosen when
`sawr.veto_loosen()` is True.

## Env

| Key | Default | Role |
|-----|---------|------|
| `PULSE_SAWR_ENABLED` | 1 | Master switch |
| `PULSE_SAWR_TARGET_WR` | 0.60 | Healthy WR target |
| `PULSE_SAWR_KILL_WR` | 0.48 | Force tighten / veto loosen |
| `PULSE_SAWR_WR_WEIGHT` | 1.0 | Utility weight on Wilson LB |
| `PULSE_SAWR_FILL_WEIGHT` | 0.35 | Utility weight on fills |
| `PULSE_SAWR_KILL_PENALTY` | 2.0 | Penalty for WR below kill |
| `PULSE_SAWR_COOLDOWN` | 5 | Settlements between adjusts |
| `PULSE_SAWR_MIN_SAMPLES` | 8 | Min n before full decide |

## Integration

- Settlement → `record_settled` + `maybe_adjust`
- Osmani pre-trade → `evaluate_pre_trade` size_mult / soft_block
- Status JSON → `sawr` block
- Persisted in ledger accounting state

## Not duplicated

| System | Role |
|--------|------|
| Binary Intel | Per-trade math + 5m TV score |
| GateAutoTuner | Hourly scalar nudges |
| **SAWR** | Meta WR/fill objective + side affinity + veto |
