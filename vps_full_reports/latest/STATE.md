# Hermes BTC Pulse — STATE (auto-generated snapshot)

_Updated each persist. Human-readable loop memory. PAPER ONLY._

- **ticks:** 2 · **last tick:** 2026-07-11 02:41 UTC

## Capital

- **starting:** $2000.0 · **on-hand (directional):** $2000.0 · **return:** 0.0%
- **total on-hand:** $2000.0 · **total return:** 0.0%
- **open exposure:** $0.0 (0 positions)

## Active strategies

- **directional:** enabled · halted=False · settled=0 · WR=None · PF=None · PnL=$0.0
- **grok decider:** mode=shadow · affects_trading=False
- **verifier (maker-checker):** enabled=False · approve_rate=None

## Verifiable stop conditions

- **directional:** halted=False · reasons=['insufficient_samples'] · metrics={'n': 0, 'wins': 0, 'win_rate': None, 'wilson_lower': None, 'breakeven_wr': None, 'profit_factor': None, 'pnl_usd': 0.0, 'max_drawdown_usd': 0.0, 'max_drawdown_pct': 0.0}

## Open positions (directional)

_none_


## Active lessons

- [`research`] Verifier veto verdict='vetoes_costing_edge' but vetoed-would-pnl=-$754 (negative) confirms the verifier is correctly blocking bad trades. Do NOT disable verifier gate until positive vetoed-would-pnl is sample-backed (n>50).
- [`research`] Core direction bot: 113 settled, 49.6% win_rate, profit_factor=0.94, -$14.93 PnL. No directional edge detected. Do NOT go live. Focus dep-arb or collect 500+ samples for tier breakdown.
- [`research`] 60s mid_convergence_rate=1.0 does NOT imply profitable exit; -10% capture_ratio shows adverse selection or latency bleed between convergence observation and fill
- [`research`] verifier_veto_quality verdict='vetoes_costing_edge' contradicts data: vetoed trades would lose $755 at 52% win; always require verifier approval until sample proves otherwise
- [`research`] nested_execute=true + clock_skew=false still allows 14k rejections for mc_adverse_selection; enable clock_skew to tighten entry timing
- [`research`] theoretical_settled=$30 vs realized=-$3 implies $33 execution drag (entry_vwap slippage + hold bleed); prioritize tighter entry_vwap bounds before expanding volume
- [`avoid`] AVOID confidence_tier=high — confidently below breakeven (WR 0.4048 vs 0.6501, n 84, EV/trade -2.1588).
- [`avoid`] AVOID hourly_entry_bucket=h15_30m — confidently below breakeven (WR 0.35 vs 0.5764, n 20, EV/trade -3.9491).

## Gates (restrict-only)

- **context_gate:** enabled=False · blocked=0 · reasons={}
- **selectivity_gate:** enabled=True · rejected=3383
- **readiness:** not_ready

