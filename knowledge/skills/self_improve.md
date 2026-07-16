# Self-Improve Skill

## Mission
Keep paper WR ≥ 80% with positive expectancy **without human babysitting**, while **never** loosening `STRICT_REAL_FREEZE` (`min_edge`, `min_conviction`, `strict_real`, κ base, risk budget, DD guards).

## Algorithms (live)

| Module | Role | Trigger |
|--------|------|---------|
| **MCHB** `autonomy/mchb.py` | Hierarchical Thompson + LinUCB explore/exploit/skip | Every signal |
| **CBPF** `autonomy/cbpf.py` | Dirichlet reliability + ridge fusion refit | Every settlement; refit /25 |
| **EHO** `autonomy/eho.py` | CMA-ES lite over mutable params only | 24h or /50 trades |
| **RASP** `autonomy/rasp.py` | HMM regime + synthetic hard examples | Every autonomy tick |
| **RGMC** `autonomy/rgmc.py` | Tighten-only soft κ / size; rollback signal | Every settlement |
| **Registry** `autonomy/registry.py` | Shadow → prod after 100 paper trades | Shadow WR≥80% |
| **Ingest** `autonomy/data_ingest.py` | Gamma/CLOB 15m + nightly bulk parquet | 15m / nightly |

## Mutable vs Frozen

**Frozen forever:** `mode=strict_real`, `min_edge=0.14`, `min_conviction=0.93`, extreme gates, `kappa_base`, `max_single_market_pct`, `risk_budget`, DD floors.

**Mutable:** `swarm_weight`, `market_blend`, TF weights, soft κ scale (≤1), size multiplier (≤1), explore rate, regime weights.

## Promote / Rollback

1. EHO or CBPF proposes mutable params → register **shadow**
2. Shadow must clear **100** paper trades at ≥80% WR → **promote**
3. If live rolling WR < **78%** (n≥25) → **rollback** + Telegram/Slack alert

## Commands

```bash
export PYTHONPATH=. HERMES_PAPER_ONLY=1 DRY_RUN=true
python -m autonomy.bootstrap          # download + pretrain
python -m autonomy.continuous         # forever loop
# or keep fleet on hermes overnight (autonomy_tick already wired)
python -m hermes.hermes_loop overnight
```

## Auto Log
