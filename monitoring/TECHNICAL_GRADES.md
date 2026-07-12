# Technical Data Grades

**Generated:** 2026-07-11T02:41:24.301178+00:00  
**Repo SHA:** `6434a6f9883f`  
**Ticks:** 2 | **Settled:** 0

## Composite

| Metric | Score | Grade |
|--------|------:|-------|
| **Composite** | **59.9** | **F** |
| Report overall | 52.8 | F |
| Technical runtime | 76.5 | C+ |

## Report scores (engine)

| Section | Score | Grade |
|---------|------:|-------|
| Trading Performance | 52.5 | F |
| Operation | 80.6 | B |
| External Signals | 25.4 | F |

## Technical runtime

_RTDS/oracle health, TV observe-only intake, design manifest compliance, pipeline integrity, gate coupling._

| Component | Score | Weight |
|-----------|------:|-------:|
| rtds_health | 100.0 | 20 |
| tv_intake | 66.0 | 20 |
| design_compliance | 69.0 | 25 |
| trade_pipeline | 90.1 | 20 |
| gate_coupling | 53.2 | 15 |

### Rtds Health (100.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| connected | 100.0 | 35 |
| oracle_fresh | 100.0 | 30 |
| stability | 100.0 | 20 |
| price_feed | 100.0 | 15 |

### Tv Intake (66.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| observe_only | 100.0 | 25 |
| alert_flow | 0.0 | 25 |
| reject_rate | 100.0 | 15 |
| trade_gates_off | 100.0 | 20 |
| mtf_freshness | 40.0 | 15 |

### Design Compliance (69.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| series_15m | 0.0 | 15 |
| green_path | 40.0 | 10 |
| paper_only | 100.0 | 10 |
| grok_shadow | 100.0 | 5 |
| tick_seconds | 100.0 | 10 |
| max_price | 50.0 | 10 |
| min_edge | 50.0 | 5 |
| min_reward_risk | 50.0 | 5 |
| cohort_relaxed | 100.0 | 10 |
| tv_trade_gates_off | 100.0 | 20 |

### Trade Pipeline (90.1)

| Component | Score | Weight |
|-----------|------:|-------:|
| accounting_integrity | 100.0 | 25 |
| lifecycle | 100.0 | 20 |
| execution_gate | 100.0 | 20 |
| recon_checks | 100.0 | 15 |
| not_halted | 100.0 | 10 |
| uptime_ticks | 1.0 | 10 |

### Gate Coupling (53.2)

| Component | Score | Weight |
|-----------|------:|-------:|
| lifecycle_funnel | 30.0 | 25 |
| exec_pass_rate | 30.0 | 25 |
| reject_diversity | 60.0 | 20 |
| cohort_session_load | 100.0 | 15 |
| recent_eval_spread | 75.0 | 15 |

## VPS score history (last entries)

| UTC | Settled | Overall | Trading | Operation | External |
|-----|--------:|--------:|--------:|----------:|---------:|
| 2026-07-11 02:40:51 UTC | 0 | 56.9 | 52.5 | 82.1 | 40.4 |
| 2026-07-11 02:40:51 UTC | 0 | 52.8 | 52.5 | 80.6 | 25.4 |
