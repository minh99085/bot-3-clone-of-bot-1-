# BTC Pulse — Full Performance Report

_Report epoch: trading metrics since **2026-07-11 02:40:51 UTC** (token `2026-07-11-btc-eth-cleanup`). Signal learning spans all eras._

_PAPER ONLY · `global_reconciled=True` · ticks 2 · primary lane: directional (1h up/down + above strike)_


## Performance Scorecard

| section | score | grade | weight |
|---|---|---|---|
| Overall | 52.8 | F | 100% |
| Trading Performance | 52.5 | F | 50% |
| Operation | 80.6 | B | 25% |
| External Signals | 25.4 | F | 25% |

### Score history (recent)

| utc | settled | trading | operation | signals | overall |
|---|---|---|---|---|---|
| 2026-07-11 02:40:51 UTC | 0 | 52.5 | 82.1 | 40.4 | 56.9 |
| 2026-07-11 02:40:51 UTC | 0 | 52.5 | 80.6 | 25.4 | 52.8 |

## 1. Trading Performance

| metric | value |
|---|---|
| Total on-hand | $2000.0 |
| Directional on-hand | $2000.0 |
| Starting capital | $2000.0 |
| Total return | 0.0% |
| Directional PnL | $0.0 |
| Total PnL | $0.0 |
| Trades / settled | 0 / 0 |
| Win rate | None |
| Win rate up / down | None / None |
| Profit factor | None |
| Avg win / avg loss | $None / $None |
| Max drawdown | $None |
| Avg PnL/trade | None |
| EV before/after cost | None / None |

### Profit discovery (5x target)

- **five_x_improvement_status:** not_proven_yet
- **improvement_ratio:** 0.0
- **baseline_total_pnl_usd:** 35.95
- **current_total_pnl_usd:** 0.0
- **directional_pnl_usd:** 0.0
- **primary_edge_source:** arbitrage
- **top_blockers:** `["total_pnl_below_5x_baseline", "insufficient_directional_sample"]`

### Accounting integrity

- **global_reconciled:** True
- **scope_note:** lifecycle counts are cumulative since canonical accounting began; baseline counts are legacy ledger totals that predate it; ledger/gate totals == baseline + accounted.
- **rejected_before_execution:** 4

### Execution gate & calibration

candidates 0 · accepted 0 · rejects `{'wide_spread': 0, 'insufficient_depth': 0, 'negative_ev_after_slippage': 0, 'too_close_to_resolution': 0, 'min_size_or_tick_violation': 0, 'partial_fill_risk': 0, 'missing_market_data': 0, 'stale_orderbook': 0, 'underdog_price_below_floor': 0}`

calibration `{'samples': 400, 'brier': 0.263169, 'log_loss': 0.830909, 'base_rate_up': 0.505, 'baseline_brier_0_5': 0.25}`

### PnL by bucket

_no bucket PnL yet_

### Selectivity impact on performance

counterfactual `{'replayed': 0, 'trades_rejected': 0, 'losses_avoided': 0, 'pnl_removed_by_rejects': 0.0, 'counterfactual_trades': 0, 'counterfactual_win_rate': None, 'counterfactual_pnl_usd': 0, 'baseline_trades': 0, 'baseline_win_rate': None, 'baseline_pnl_usd': 0, 'reject_reasons_by_bucket': {}, 'note': 'in-sample replay using final accumulated bucket evidence (diagnostic estimate)'}`
| dim | bucket | n | WR | breakeven | EV/trade | blocked |
|---|---|---|---|---|---|---|
| hourly_entry_bucket | h15_30m | 20 | 0.35 | 0.5764 | -3.9491 | True |
| confidence_tier | high | 84 | 0.4048 | 0.6501 | -2.1588 | True |
| markov_state | stale_polymarket_down | 21 | 0.2381 | 0.4238 | -1.8998 | True |
| confidence_tier | medium | 43 | 0.3721 | 0.516 | -1.3485 | True |
| hourly_entry_bucket | na | 83 | 0.494 | 0.538 | -0.8026 | False |
| zscore_bucket | -2..-1 | 41 | 0.439 | 0.4572 | -0.3012 | False |
| zscore_bucket | na | 50 | 0.46 | 0.5032 | -0.2811 | False |
| hurst_regime | insufficient_data | 71 | 0.4789 | 0.4757 | 0.028 | False |

### Recent positions

_no positions_

## 2. Operation


### Engine health

- **ticks:** 2
- **global_reconciled:** True
- **paper_only:** True
- **live_trading_enabled:** False
- **sample_sizes:** `{"accepted": 0, "settled": 0, "candidates": 8, "edge_model_labeled": 262}`
- **status:** not_ready
- **reason:** None
- **checks:** None

### Candidate lifecycle

created 8 · terminals `{'accepted': 0, 'rejected': 4, 'skipped': 0, 'expired': 0, 'missing_data': 4}`

rejected_by_stage `{'directional': 0, 'execution_gate': 0, 'tier_engine': 4}`

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

count 300
- [`research`] 60s mid_convergence=100% does NOT guarantee capture if entry VWAP is wide; capture_ratio=-0.10 vs theoretical $30 means slippage or adverse selection at entry dominates. Lower max_entry_vwap or add pre-entry timing filter.
- [`research`] mc_adverse_selection rejections (10k+) vastly exceed settled (180), indicating maker-checker is catching most adverse flow. If this persists post-VWAP tightening, consider gating on pre-execution mid stability (e.g., 5s rolling vol < threshold).
- [`research`] Verifier veto verdict='vetoes_costing_edge' but vetoed-would-pnl=-$754 (negative) confirms the verifier is correctly blocking bad trades. Do NOT disable verifier gate until positive vetoed-would-pnl is sample-backed (n>50).
- [`research`] Core direction bot: 113 settled, 49.6% win_rate, profit_factor=0.94, -$14.93 PnL. No directional edge detected. Do NOT go live. Focus dep-arb or collect 500+ samples for tier breakdown.
- [`research`] 60s mid_convergence_rate=1.0 does NOT imply profitable exit; -10% capture_ratio shows adverse selection or latency bleed between convergence observation and fill
- [`research`] verifier_veto_quality verdict='vetoes_costing_edge' contradicts data: vetoed trades would lose $755 at 52% win; always require verifier approval until sample proves otherwise
- [`research`] nested_execute=true + clock_skew=false still allows 14k rejections for mc_adverse_selection; enable clock_skew to tighten entry timing
- [`research`] theoretical_settled=$30 vs realized=-$3 implies $33 execution drag (entry_vwap slippage + hold bleed); prioritize tighter entry_vwap bounds before expanding volume
- [`avoid`] AVOID confidence_tier=high — confidently below breakeven (WR 0.4048 vs 0.6501, n 84, EV/trade -2.1588).
- [`avoid`] AVOID hourly_entry_bucket=h15_30m — confidently below breakeven (WR 0.35 vs 0.5764, n 20, EV/trade -3.9491).

### Internal gates & allowlist

- **decision_rule:** confidently_below_breakeven_and_pf_floor_fdr
- **accepted:** 2901
- **rejected:** 3383
- **explored:** 28
- **block_reasons:** None
- **enabled:** False
- **explore_rate:** 0.25
- **explored:** 0
- **blocked:** 0
- **enabled:** True
- **active:** True
- **weight:** 0.5
- **reason:** active
- **enabled:** None
- **halted_directional:** None
- **rolling_profit_factor:** None
- **rolling_win_rate:** None

### Grok decider (operations)

- **mode:** shadow
- **affects_trading:** False
- **decided:** 3648
- **errors:** 175
- **avg_latency_s:** 7.435
- **abstains:** 965

## 3. External Signals


### Signal impact on trading performance

| signal | value |
|---|---|
| TV aligned bot WR | 0.4762 |
| TV opposed bot WR | 0.4815 |
| TV signal hit-rate | 0.4889 |
| TV settled w/ signal | 95 |
| TV edge verdict | no_directional_edge |
| Grok direction accuracy | 0.3446 |
| Grok view accuracy | 0.4597 |
| CEX-lead proven edge | None |

### TradingView

- **tradingview_alerts_received:** 0
- **tradingview_alerts_valid:** 0
- **tradingview_alerts_rejected:** 0
- **tradingview_mtf_confirmation:** `{"symbol": "BTCUSD", "mtf_timeframes": ["5", "10", "15", "20", "25", "30", "35", "40", "45", "50", "55", "60"], "mtf_count": 12, "confirm_windows_by_tf": {"5": 1500.0, "10": 1500.0, "15": 2250.0, "20": 3000.0, "25": 3750.0, "30": 4500.0, "35": 5250.0, "40": 6000.0, "45": 6750.0, "50": 7500.0, "55": 8250.0, "60": 9000.0}, "fast_pair": ["5", "10"], "trend_by_tf": {}, "tf_5m_dir": null, "tf_5m_age_s": null, "tf_10m_dir": null, "tf_10m_age_s": null, "tf_15m_dir": null, "tf_15m_age_s": null, "tf_20m_dir": null, "tf_20m_age_s": null, "tf_25m_dir": null, "tf_25m_age_s": null, "tf_30m_dir": null, "tf_`

settled_with_signal 95

best_buckets `[{"dimension": "indicator_name", "bucket": "Hermes BTC Pulse Lite", "n": 47, "win_rate": 0.4468, "pnl_usd": -10.3113, "avg_ev_after_cost": 0.099368, "all_reconciled": true}, {"dimension": "composite_version", "bucket": "lite-2", "n": 47, "win_rate": 0.4468, "pnl_usd": -10.3113, "avg_ev_after_cost": 0.099368, "all_reconciled": true}, {"dimension": "signal_level", "bucket": "DOWN_WEAK", "n": 14, "win_rate": 0.5, "pnl_usd": 4.1567, "avg_ev_after_cost": 0.096238, "all_reconciled": true}, {"dimension": "hurst_regime", "bucket": "noise", "n": 3, "win_rate": 0.6667, "pnl_usd": 5.1373, "avg_ev_after_cost": 0.089733, "all_reconciled": true}, {"dimension": "signal_level", "bucket": "UP_STRONG", "n": 27, "win_rate": 0.4815, "pnl_usd": 94.5051, "avg_ev_after_cost": 0.088789, "all_reconciled": true}]`

worst_buckets `[{"dimension": "zscore_bucket", "bucket": "1..2", "n": 3, "win_rate": 0.3333, "pnl_usd": -20.1649, "avg_ev_after_cost": 0.0127, "all_reconciled": true}, {"dimension": "composite_version", "bucket": "rsi_div_builtin_v1", "n": 5, "win_rate": 0.4, "pnl_usd": -25.1026, "avg_ev_after_cost": 0.021, "all_reconciled": true}, {"dimension": "indicator_name", "bucket": "RSI Divergence Indicator", "n": 5, "win_rate": 0.4, "pnl_usd": -25.1026, "avg_ev_after_cost": 0.021, "all_reconciled": true}, {"dimension": "zscore_bucket", "bucket": "<=-2", "n": 8, "win_rate": 0.625, "pnl_usd": 12.0111, "avg_ev_after_cost": 0.021012, "all_reconciled": true}, {"dimension": "composite_version", "bucket": "mr1-3m", "n": 3, "win_rate": 0.6667, "pnl_usd": 1.8946, "avg_ev_after_cost": 0.026667, "all_reconciled": true}]`

rsi_trend hit_rate 0.4915 (n 6181)

**context_gate:** enabled=False blocked=0 explored=0 `{}`

**down_bias_gate:** enabled=False blocked=0 explored=0 `{}`

**mtf_gate:** enabled=False blocked=0 explored=0 `{}`

**signal_gate:** enabled=False blocked=None explored=None `None`

### Grok Decision Engine (signal quality)

- **mode:** shadow
- **affects_trading:** False
- **direction_accuracy:** 0.3446
- **brier:** 0.2644
- **view_accuracy:** 0.4597
- **view_brier:** 0.2622
- **views_graded:** 1142
- **view_edge_candidates:** `[]`

accuracy_by_context `{"hurst_regime": {"insufficient_data": {"n": 224, "accuracy": 0.4732}, "trending": {"n": 745, "accuracy": 0.451}, "noise": {"n": 125, "accuracy": 0.448}, "mean_reverting": {"n": 48, "accuracy": 0.5625}}, "markov_state": {"chop_noise": {"n": 1012, "accuracy": 0.4555}, "stale_polymarket_up": {"n": 32, "accuracy": 0.4375}, "stale_polymarket_down": {"n": 48, "accuracy": 0.4792}, "liquidity_danger": {"n": 50, "accuracy": 0.54}}, "ttc_bucket": {">=240s": {"n": 1138, "accuracy": 0.4578}, "<60s": {"n": 3, "accuracy": 1.0}, "120-240s": {"n": 1, "accuracy": 1.0}}, "conviction_bucket": {"coinflip": {"n": 1091, "accuracy": 0.4482}, "lean": {"n": 30, "accuracy": 0.6}, "strong": {"n": 21, "accuracy": 0.8571}}}`

recent_decisions `[{"action": "no_trade", "p_up": 0.49, "confidence": 0.0, "outcome_up": true, "view_correct": false, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.5, "confidence": 0.0, "outcome_up": true, "view_correct": false, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.5, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.51, "confidence": 0.0, "outcome_up": true, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "conviction_bucket": "coi`

### Grok signal intel (analyst + predictor)

budget `{'daily_usd_cap': 35.0, 'est_usd_per_call': 0.02, 'spent_today_usd': 0.04, 'calls_today': 2, 'per_feature_hourly': {'predictor': 60, 'analyst': 4, 'overlay': 20, 'decider': 120, 'news': 40}}`

predictor_B `{'enabled': False}`

analyst_A last_note `null`

### CEX-lead latency edge

_disabled_

### Pulse edge signal

`{"enabled": true, "observe_only": true, "report_only": true, "affects_trading": false, "settled": 400, "by_stale_divergence": {"na": {"n": 143, "win_rate": 0.5245, "pnl_usd": -41.5764, "avg_ev_after_cost": 0.079937, "all_reconciled": true}, "not_stale": {"n": 228, "win_rate": 0.4956, "pnl_usd": 477.5803, "avg_ev_after_cost": 0.066923, "all_reconciled": true}, "stale_polymarket_down": {"n": 9, "win_rate": 0.5556, "pnl_usd": -0.3266, "avg_ev_after_cost": 0.060414, "all_reconciled": true}, "stale_polymarket_up": {"n": 10, "win_rate": 0.2, "pnl_usd": -188.2875, "avg_ev_after_cost": 0.068023, "all_reconciled": true}, "already_priced": {"n": 10, "win_rate": 0.3, "pnl_usd": 100.8562, "avg_ev_after_cost": 0.055061, "all_reconciled": true}}, "by_ttc_bucket": {"na": {"n": 143, "win_rate": 0.5245, "p`

### DOWN stack grader

`{"observe_only": true, "affects_trading": false, "min_samples": 30, "edge_margin": 0.04, "buckets": [{"bucket": "other", "n": 391, "win_rate": 0.4936, "wilson_lower": 0.4523, "avg_entry": 0.5034, "breakeven_wr": 0.5034, "pnl_usd": 348.5726, "proven": false}, {"bucket": "stale_down_only", "n": 9, "win_rate": 0.5556, "wilson_lower": 0.3041, "avg_entry": 0.4871, "breakeven_wr": 0.4871, "pnl_usd": -0.3266, "proven": false}], "any_proven": false, "proven_buckets": [], "promotion_rule": "n>=30 AND wilson_lower>avg_entry+0.04 AND pnl>0"}`
