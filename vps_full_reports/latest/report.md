# BTC Pulse — Full Performance Report

_PAPER ONLY · `global_reconciled=True` · ticks 1407 · primary lane: directional (1h up/down + above strike)_


## Performance Scorecard

| section | score | grade | weight |
|---|---|---|---|
| Overall | 67.2 | D | 100% |
| Trading Performance | 73.5 | C | 50% |
| Operation | 86.5 | B+ | 25% |
| External Signals | 35.5 | F | 25% |

### Score history (recent)

| utc | settled | trading | operation | signals | overall |
|---|---|---|---|---|---|
| 2026-07-12 07:34:05 UTC | 2 | 72.6 | 84.4 | 35.5 | 66.3 |
| 2026-07-12 07:47:57 UTC | 2 | 62.6 | 64.7 | 35.5 | 56.4 |
| 2026-07-12 07:49:19 UTC | 3 | 74.7 | 84.7 | 35.5 | 67.4 |
| 2026-07-12 07:54:13 UTC | 3 | 64.7 | 64.8 | 35.5 | 57.4 |
| 2026-07-12 08:00:21 UTC | 4 | 64.4 | 84.8 | 35.5 | 62.3 |
| 2026-07-12 08:04:21 UTC | 5 | 48.2 | 84.9 | 35.5 | 54.2 |
| 2026-07-12 08:34:36 UTC | 5 | 48.2 | 85.7 | 35.5 | 54.4 |
| 2026-07-12 09:04:39 UTC | 5 | 48.2 | 86.4 | 35.5 | 54.6 |
| 2026-07-12 09:34:39 UTC | 5 | 48.2 | 86.5 | 35.5 | 54.6 |
| 2026-07-12 09:35:10 UTC | 6 | 58.3 | 86.5 | 35.5 | 59.6 |
| 2026-07-12 10:05:12 UTC | 6 | 58.3 | 86.5 | 35.5 | 59.6 |
| 2026-07-12 10:30:42 UTC | 7 | 65.0 | 86.5 | 35.5 | 63.0 |
| 2026-07-12 10:45:58 UTC | 8 | 69.5 | 86.5 | 35.5 | 65.2 |
| 2026-07-12 11:06:09 UTC | 9 | 73.5 | 86.5 | 35.5 | 67.2 |
| 2026-07-12 11:36:23 UTC | 9 | 73.5 | 86.5 | 35.5 | 67.2 |

## 1. Trading Performance

| metric | value |
|---|---|
| Total on-hand | $2020.56 |
| Directional on-hand | $2020.56 |
| Starting capital | $2000.0 |
| Total return | 1.03% |
| Directional PnL | $20.56 |
| Total PnL | $20.56 |
| Trades / settled | 9 / 9 |
| Win rate | 0.7778 |
| Win rate up / down | 1.0 / 0.7143 |
| Profit factor | 2.3554 |
| Avg win / avg loss | $5.1045 / $7.585 |
| Max drawdown | $15.17 |
| Avg PnL/trade | 2.2846 |
| EV before/after cost | None / None |

### Performance by market (concise)

| market | settled | WR | PF | PnL | UP WR | DOWN WR |
|---|---|---|---|---|---|---|
| 15m | 5 | 0.8 | 2.6351 | $13.1133 | 1.0 | 0.75 |
| 15m | 4 | 0.75 | 2.0417 | $7.4484 | 1.0 | 0.6667 |

### Profit discovery (5x target)

- **five_x_improvement_status:** not_proven_yet
- **improvement_ratio:** 0.5719
- **baseline_total_pnl_usd:** 35.95
- **current_total_pnl_usd:** 20.5616
- **directional_pnl_usd:** 20.5616
- **primary_edge_source:** directional
- **top_blockers:** `["total_pnl_below_5x_baseline"]`

### Accounting integrity

- **global_reconciled:** True
- **scope_note:** lifecycle counts are cumulative since canonical accounting began; baseline counts are legacy ledger totals that predate it; ledger/gate totals == baseline + accounted.
- **rejected_before_execution:** 354

### Execution gate & calibration

candidates 9 · accepted 9 · rejects `{'wide_spread': 0, 'insufficient_depth': 0, 'negative_ev_after_slippage': 0, 'too_close_to_resolution': 0, 'min_size_or_tick_violation': 0, 'partial_fill_risk': 0, 'missing_market_data': 0, 'stale_orderbook': 0, 'underdog_price_below_floor': 0}`

calibration `{'samples': 0, 'brier': None, 'log_loss': None, 'base_rate_up': None, 'baseline_brier_0_5': 0.25}`

### PnL by bucket

_no bucket PnL yet_

### Selectivity impact on performance

counterfactual `{'replayed': 9, 'trades_rejected': 0, 'losses_avoided': 0, 'pnl_removed_by_rejects': 0.0, 'counterfactual_trades': 9, 'counterfactual_win_rate': 0.7778, 'counterfactual_pnl_usd': 20.5616, 'baseline_trades': 9, 'baseline_win_rate': 0.7778, 'baseline_pnl_usd': 20.5616, 'reject_reasons_by_bucket': {}, 'note': 'in-sample replay using final accumulated bucket evidence (diagnostic estimate)'}`

### Recent positions

| mkt | window | side | entry_mode | entry | fair | outcome | won | pnl |
|---|---|---|---|---|---|---|---|---|
| 5m | , 6:45AM-7:00AM ET | down | osmani_lane | 0.52 | 0.4319243711433829 | down | ✓ | 4.735385 |
| 5m | , 6:30AM-6:45AM ET | down | osmani_lane | 0.53 | 0.44991956681349965 | down | ✓ | 4.194528 |
| 5m | , 6:15AM-6:30AM ET | down | osmani_lane | 0.52 | 0.4291838538236614 | down | ✓ | 4.772308 |
| 5m | , 5:15AM-5:30AM ET | down | osmani_lane | 0.53 | 0.45049607373619494 | down | ✓ | 4.18566 |
| 5m | , 3:45AM-4:00AM ET | down | osmani_lane | 0.53 | 0.3904448088979904 | up | ✗ | -8.02 |
| 5m | , 3:45AM-4:00AM ET | down | osmani_lane | 0.53 | 0.43260317062127474 | up | ✗ | -7.15 |
| 5m | , 3:30AM-3:45AM ET | up | osmani_lane | 0.54 | 0.55967066669583 | up | ✓ | 5.775556 |
| 5m | , 2:45AM-3:00AM ET | up | osmani_lane | 0.55 | 0.6092550912889555 | up | ✓ | 6.218182 |
| 5m | , 2:15AM-2:30AM ET | down | osmani_lane | 0.55 | 0.41236890018342354 | down | ✓ | 5.85 |

## 2. Operation


### Engine health

- **ticks:** 1407
- **global_reconciled:** True
- **paper_only:** True
- **live_trading_enabled:** False
- **sample_sizes:** `{"accepted": 0, "settled": 9, "candidates": 5321, "edge_model_labeled": 0}`
- **status:** not_ready
- **reason:** None
- **checks:** None

### Candidate lifecycle

created 5321 · terminals `{'accepted': 0, 'rejected': 4967, 'skipped': 2, 'expired': 0, 'missing_data': 352}`

rejected_by_stage `{'directional': 0, 'execution_gate': 0, 'tier_engine': 4869, 'lane_15m': 77, 'hourly_chart_lean': 18, 'p_exec': 3}`

### Looping engine (sub-loops)

| loop | role | trigger | interval_s | stop | status |
|---|---|---|---|---|---|
| data_ingestion | data | tick | None | None | True |
| directional | strategy | per_window | None | warming_up(n<60) | True |
| execution | execute | per_decision | None | fill or reject | — |
| heartbeat | automation | tick | 15.0 | process running | — |
| lessons | memory | per_settlement | None | None | — |
| loop_synthesis | loop_engine(WS5) | per_light_report | None | evidence-gated next experiment | — |
| news | context | interval | 300.0 | None | — |
| osmani_discovery | discovery_lane | timer | 60.0 | circuit_breaker | — |
| osmani_execution | execution_lane | queue | None | circuit_breaker | — |
| osmani_ledger | ledger_lane | queue | None | disk_write_ok | — |
| research_meta | research(/goal) | interval | 1200.0 | verifiable metric improvement | — |
| risk_monitor | risk | per_settlement | None | None | — |
| signal_generation | signal | per_window | None | None | True |
| tradingview | context | webhook | None | observe-only context feed | True |
| verifier | verify(maker-checker) | per_decision | None | approve/veto verdict | — |

### Maker-checker verifier

- **enabled:** False
- **verified:** None
- **approvals:** None
- **vetoes:** None
- **errors:** None
- **approve_rate:** None
- **avg_latency_s:** None

### Research meta-loop

- **enabled:** False
- **calls:** None
- **auto_apply:** None
- **lessons_added:** None

### Compounding lessons

count 0

### Internal gates & allowlist

- **decision_rule:** confidently_below_breakeven_and_pf_floor_fdr
- **accepted:** 0
- **rejected:** 0
- **explored:** 0
- **block_reasons:** None
- **enabled:** False
- **explore_rate:** 0.12
- **explored:** 0
- **blocked:** 0
- **enabled:** True
- **active:** False
- **weight:** 0.0
- **reason:** insufficient_samples
- **enabled:** None
- **halted_directional:** None
- **rolling_profit_factor:** None
- **rolling_win_rate:** None

### Grok decider (operations)

- **mode:** shadow
- **affects_trading:** False
- **decided:** 113
- **errors:** 0
- **avg_latency_s:** 4.856
- **abstains:** 56

## 3. External Signals


### Signal impact on trading performance

| signal | value |
|---|---|
| TV aligned bot WR | None |
| TV opposed bot WR | None |
| TV signal hit-rate | None |
| TV settled w/ signal | 0 |
| TV edge verdict | insufficient_evidence |
| Grok direction accuracy | None |
| Grok view accuracy | 0.4286 |
| CEX-lead proven edge | None |

### TradingView

- **tradingview_alerts_received:** 0
- **tradingview_alerts_valid:** 0
- **tradingview_alerts_rejected:** 0
- **tradingview_mtf_confirmation:** `{"symbol": "BTCUSD", "mtf_timeframes": ["5", "10", "15", "20", "25", "30", "35", "40", "45", "50", "55", "60"], "mtf_count": 12, "confirm_windows_by_tf": {"5": 1500.0, "10": 1500.0, "15": 2250.0, "20": 3000.0, "25": 3750.0, "30": 4500.0, "35": 5250.0, "40": 6000.0, "45": 6750.0, "50": 7500.0, "55": 8250.0, "60": 9000.0}, "fast_pair": ["5", "10"], "trend_by_tf": {}, "tf_5m_dir": null, "tf_5m_age_s": null, "tf_10m_dir": null, "tf_10m_age_s": null, "tf_15m_dir": null, "tf_15m_age_s": null, "tf_20m_dir": null, "tf_20m_age_s": null, "tf_25m_dir": null, "tf_25m_age_s": null, "tf_30m_dir": null, "tf_`

settled_with_signal 0

best_buckets `[]`

worst_buckets `[]`

rsi_trend hit_rate None (n 0)

**context_gate:** enabled=False blocked=0 explored=0 `{}`

**down_bias_gate:** enabled=False blocked=0 explored=0 `{}`

**mtf_gate:** enabled=False blocked=0 explored=0 `{}`

**signal_gate:** enabled=False blocked=None explored=None `None`

### Grok Decision Engine (signal quality)

- **mode:** shadow
- **affects_trading:** False
- **direction_accuracy:** None
- **brier:** None
- **view_accuracy:** 0.4286
- **view_brier:** 0.2583
- **views_graded:** 56
- **view_edge_candidates:** `[]`

accuracy_by_context `{"hurst_regime": {"insufficient_data": {"n": 6, "accuracy": 0.3333}, "trending": {"n": 50, "accuracy": 0.44}}, "markov_state": {"chop_noise": {"n": 56, "accuracy": 0.4286}}, "ttc_bucket": {">=240s": {"n": 56, "accuracy": 0.4286}}, "conviction_bucket": {"lean": {"n": 5, "accuracy": 0.2}, "coinflip": {"n": 51, "accuracy": 0.451}}}`

recent_decisions `[{"action": "no_trade", "p_up": 0.492, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.48, "confidence": 0.0, "outcome_up": true, "view_correct": false, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.484, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.533, "confidence": 0.0, "outcome_up": true, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "conviction_bucket":`

### Grok signal intel (analyst + predictor)

budget `{'daily_usd_cap': 35.0, 'est_usd_per_call': 0.02, 'spent_today_usd': 4.48, 'calls_today': 224, 'per_feature_hourly': {'predictor': 60, 'analyst': 4, 'overlay': 20, 'decider': 120, 'news': 40}}`

predictor_B `{'enabled': False}`

analyst_A last_note `null`

### CEX-lead latency edge

_disabled_

### Pulse edge signal

`{"enabled": true, "observe_only": true, "report_only": true, "affects_trading": false, "settled": 0, "by_stale_divergence": {}, "by_ttc_bucket": {}, "by_ob_pressure": {}, "by_edge_score": {}, "by_cex_agreement": {}}`

### DOWN stack grader

`{"observe_only": true, "affects_trading": false, "min_samples": 30, "edge_margin": 0.04, "buckets": [], "any_proven": false, "proven_buckets": [], "promotion_rule": "n>=30 AND wilson_lower>avg_entry+0.04 AND pnl>0"}`
