# Env coupling rules (pulse bot)

## TV context max TTC × baseline cohort band

When **both** are enabled:

- `PULSE_BASELINE_COHORT_GATE_ENABLED=1`
- `PULSE_TV_CONTEXT_GATE=1` (default on in loop-arch)

the context gate blocks entries with `ttc_s >= PULSE_TV_CONTEXT_MAX_TTC_S`, while the
baseline cohort only allows a scaled TTC band:

| Market | Scale | Cohort band (base 180–240s) |
|--------|-------|-----------------------------|
| 5m | ×1 | 180–240s |
| 15m | ×3 | 540–720s |

**Rule:** `PULSE_TV_CONTEXT_MAX_TTC_S` must be **strictly greater** than the scaled cohort
maximum on every active series slug in `PULSE_SERIES_SLUGS`.

For dual 5m+15m loop-arch: use **900** (≥ 720 required, matches 15m window length).

### Deadlock example (do not use)

```
PULSE_TV_CONTEXT_MAX_TTC_S=180
PULSE_BASELINE_COHORT_TTC_MIN_S=180
PULSE_BASELINE_COHORT_TTC_MAX_S=240
PULSE_SERIES_SLUGS=btc-up-or-down-5m,btc-up-or-down-15m
```

→ zero quant-path trades; `tv_context_ttc_too_far` spikes in lifecycle.

### Enforcement

| Layer | Behavior |
|-------|----------|
| `engine/pulse/config_coupling.py` | Computes required min; auto-clamps runtime effective max |
| `scripts/apply-loop-arch-env.py` | Auto-raises env value before write |
| `config_coupling` in status API | `configured_ok`, `effective_s`, `fix_hint` |
| `scan-health.py` | P0 if `configured_ok` is false |

### After `.env` changes

```bash
docker compose up -d --force-recreate hermes-training
```

Restart alone does not reload env.