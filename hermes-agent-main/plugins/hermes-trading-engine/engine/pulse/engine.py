"""BTC 5-minute pulse paper-trading engine (orchestrator).

One ``tick`` (run every few seconds): poll the BTC price, refresh the rolling 5-min
windows, snapshot each window's open price, price each open window as a digital option,
take LOOSENED paper trades, and settle/calibrate closed windows. Writes a status JSON +
paper ledger every tick.

PAPER ONLY: no order client, no wallet, no signing anywhere in this engine.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine.pulse.markets import SERIES_SLUG_5M, SERIES_SLUG_15M
from engine.pulse.price import PulsePriceFeed, build_price_source
from engine.pulse.fair_value import RollingVol, digital_p_up
from engine.pulse.strategy import decide
from engine.pulse.execution_gate import evaluate_execution
from engine.pulse.executor import PulseLedger
from engine.pulse.decisions import (MarketContext, CandidateDecision, ExecutionCostEstimate,
                                     TradeAction, RejectAction, PaperFill, DecisionResult,
                                     LifecycleReconciler, ttc_bucket, half_life_bucket)
from engine.pulse.reporting import (spread_bucket as _spread_bucket,
                                     depth_bucket as _depth_bucket,
                                     confidence_tier as _confidence_tier)
from engine.pulse.settlement import (PulseCalibration, resolve_window, proxy_outcome)
from engine.pulse.reconciliation import (GateObservations, capture_baseline, empty_baseline)

logger = logging.getLogger("hte.pulse.engine")
DIRECTIONAL_LEARNING_VERSION = "directional_v2_20260711"


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _parse_tv_mtf_timeframes(raw) -> tuple[str, ...]:
    from engine.pulse.tradingview import parse_mtf_timeframes
    return parse_mtf_timeframes(raw)


def _parse_tv_drop_timeframes(raw) -> tuple[str, ...]:
    """Parse ``PULSE_TV_DROP_TIMEFRAMES`` (retired chart TFs). Empty -> drop nothing (unlike the MTF
    parser, which falls back to a default set)."""
    from engine.pulse.tradingview import normalize_timeframe
    if raw is None or not str(raw).strip():
        return ()
    out: list[str] = []
    for part in str(raw).split(","):
        tf = normalize_timeframe(part.strip())
        if tf and tf not in out:
            out.append(tf)
    return tuple(out)


def _tv_mtf_confirm_windows(cfg: "PulseConfig") -> dict[str, float]:
    from engine.pulse.tradingview import build_mtf_confirm_windows
    return build_mtf_confirm_windows(
        cfg.tradingview_mtf_timeframes,
        legacy_5m_s=cfg.tradingview_mtf_confirm_window_s,
        legacy_10m_s=cfg.tradingview_mtf_confirm_window_10m_s,
        legacy_15m_s=cfg.tradingview_mtf_confirm_window_15m_s,
        overrides={
            "2": cfg.tradingview_mtf_confirm_window_2m_s,
            "3": cfg.tradingview_mtf_confirm_window_3m_s,
            "4": cfg.tradingview_mtf_confirm_window_4m_s,
            "5": cfg.tradingview_mtf_confirm_window_5m_s,
            "13": cfg.tradingview_mtf_confirm_window_13m_s,
            "30": cfg.tradingview_mtf_confirm_window_30m_s,
            "45": cfg.tradingview_mtf_confirm_window_45m_s,
            "55": cfg.tradingview_mtf_confirm_window_55m_s,
        },
    )


@dataclass
class PulseConfig:
    tick_seconds: float = 4.0
    size_usd: float = 5.0
    min_edge: float = 0.03
    min_seconds_to_close: float = 4.0
    min_depth_usd: float = 1.0
    edge_buffer: float = 0.01
    max_price: float = 0.97
    # minimum reward-to-risk for a paper entry: at ask ``p`` a win nets (1-p)/p per $ staked while a
    # loss costs the full stake. 0.0 = off (default). e.g. 0.25 => skip entries priced above ~0.80
    # (which would win < ~$1.25 per $5 risked) so one loss can't wipe ~10 tiny wins. PAPER ONLY.
    min_reward_risk: float = 0.0
    # extra reward/risk floor for UP entries (asymmetric bleed guard; DOWN keeps base only).
    min_reward_risk_up_premium: float = 0.15
    max_open_lag_s: float = 20.0
    max_open_lag_15m_s: float = 240.0
    vol_window_s: float = 900.0
    settle_grace_s: float = 180.0          # prefer authoritative Polymarket(Chainlink) before proxy
    max_positions_kept: int = 500
    fresh_start: bool = False
    # trade-quality / expectancy gates
    min_seconds_since_open: float = 30.0   # skip the dead early window (digital ~0.5 noise)
    min_vol_samples: int = 12              # need a real vol estimate before trusting P(up)
    sigma_trust_floor: float = 2.0e-6      # below this, price is too flat -> digital untrusted
    basis_buffer: float = 0.02             # cover Coinbase-vs-Chainlink resolution basis drift
    # Grok event-risk overlay (advisory; can only make the bot MORE cautious)
    grok_overlay_enabled: bool = False
    grok_overlay_interval_s: float = 180.0
    grok_overlay_max_calls_per_hour: int = 20
    # Grok signal-intelligence layer (OBSERVE-ONLY, off hot path): A = batch analyst over the
    # TradingView signal-learning report; B = per-signal P(up) predictor graded vs realized move.
    # A shared budget caps daily cost + per-feature hourly calls. Neither can trade.
    grok_signal_analyst_enabled: bool = False        # A
    grok_signal_predictor_enabled: bool = False       # B
    grok_analyst_interval_s: float = 1800.0
    grok_budget_daily_usd: float = 50.0
    grok_est_usd_per_call: float = 0.02
    grok_predictor_max_calls_per_hour: int = 90
    grok_analyst_max_calls_per_hour: int = 8
    # ---- Grok DECISION ENGINE ("Grok decides, bot executes"; PAPER ONLY) ----
    # mode: off | shadow (decide+grade only, no trade — safe default). Follow mode removed.
    grok_decider_mode: str = "shadow"        # observe-only by default (grades, never affects trading)
    grok_decider_model: str = "grok-4.3"
    grok_decider_timeout_s: float = 12.0
    grok_decider_use_search: bool = False            # enable xAI live web/X news search (slower/$$)
    grok_decider_min_confidence: float = 0.55
    grok_decider_ttl_s: float = 240.0
    grok_decider_max_calls_per_hour: int = 200
    grok_tiered_compute_enabled: bool = True
    grok_tier_full_divergence_min: float = 0.025
    grok_tier_deep_divergence_min: float = 0.04
    # FOLLOW exploration (removed with follow mode; kept at 0 for env compat)
    grok_decider_explore_rate: float = 0.0
    grok_decider_explore_size_fraction: float = 0.5
    # Minimum |p_up - 0.5| required before an explore trade on Grok's abstain view (blocks coin-flip).
    grok_decider_explore_min_view_margin: float = 0.08
    # minimum P(UP wins) before any Grok-owned UP trade (follow/explore/adaptive/mispricing).
    grok_up_min_p_win: float = 0.58
    # adaptive self-improvement loop: auto-EXPLOIT contexts with a proven view-edge (Wilson lower >
    # 0.5), AVOID proven-losing contexts, and only EXPLORE the uncertain ones. Default ON.
    grok_decider_adaptive: bool = True
    # ---- #1 maker-checker VERIFIER (independent Claude model) + #4 research meta-loop ----
    verifier_enabled: bool = True          # maker-checker ON for paper by default (needs ANTHROPIC key)
    verifier_fail_open: bool = True          # no verdict in time -> approve (don't freeze) but log
    # FOLLOW trades wait for the actual Claude verdict (fail-CLOSED on pending) so the maker-checker
    # genuinely gates them rather than fail-opening before the async worker finishes.
    verifier_follow_require_verdict: bool = True
    verifier_explore_approve: bool = False   # WS2: shrunk approve over veto for exploration trades
    verifier_explore_max_size_fraction: float = 0.5
    verifier_max_calls_per_hour: int = 120
    research_loop_enabled: bool = False
    research_interval_s: float = 1800.0      # idle FLOOR; the loop is mainly EVENT-triggered
    research_event_min_gap_s: float = 600.0  # min gap between event-triggered research runs
    research_auto_apply: bool = False        # WS2: default observe-only; avoid blocks only when on
    research_forbid_size_increase: bool = True  # WS2: research_meta must never bump directional size
    research_avoid_max: int = 14             # cap on active research avoid-context rules
    research_exploit_max: int = 10           # cap on active research EXPLOIT-context rules
    lessons_revalidate_ttl_s: float = 21600.0  # avoid/exploit lesson retracts if unconfirmed this long
    research_exploit_size_mult: float = 1.5  # size-up multiplier for proven-winning exploit contexts
    research_max_calls_per_hour: int = 6
    claude_budget_daily_usd: float = 10.0
    claude_est_usd_per_call: float = 0.01
    grok_news_enabled: bool = True                   # periodic web/X news digest (advisory context only)
    grok_news_refresh_s: float = 300.0               # periodic web/X news digest cadence
    # price feed: 'auto' uses Chainlink Data Streams (exact resolution feed) when creds are
    # set, else the Coinbase proxy. A sub-second background sampler keeps the price fresh
    # between the slower trade ticks.
    price_source: str = "auto"
    price_sampler_interval_s: float = 1.0
    # ---- oracle reference model (Chainlink Data Streams ref price via Polymarket RTDS) ----
    oracle_feed_type: str = "chainlink_data_streams_refprice"
    oracle_symbol: str = "btc/usd"
    fast_feeds: tuple = ("binance_btcusdt", "coinbase_btcusd")
    settlement_source_priority: tuple = ("polymarket_resolution",)
    proxy_max_close_lag_s: float = 30.0
    rtds_enabled: bool = True
    rtds_max_age_s: float = 45.0             # RTDS oracle tick older than this -> feed gets None (stale)
    price_max_age_s: float = 60.0            # abstain ("stale_price") if the price feed is older than this
    # strict execution-quality gate (orderbook-reality EV after VWAP/slippage)
    exec_max_spread: float = 0.06
    exec_min_order_usd: float = 1.0
    exec_max_depth_consume_frac: float = 0.5
    exec_min_ev_after_slippage: float = 0.02   # require a real calibrated edge buffer (per-share)
    # don't BUY the underdog side (VWAP fill below this) on opinion paths — the price is the best
    # probability and the bot's model has negative edge on cheap/tail sides (live: underdog buys
    # ~28% win = the entire net loss; favourites >0.5 were net-positive). Proven edges are exempt.
    min_entry_price: float = 0.50
    exec_max_book_age_s: float = 30.0        # reject stale orderbook older than this
    research_features_enabled: bool = True   # OBSERVE-ONLY EP Chan features (never trade)
    # OBSERVE-ONLY BTC Pulse Edge Signal layer (CEX basket momentum + stale-price divergence +
    # orderbook pressure + pulse_edge_score). Never trades/vetoes/bypasses the gate.
    edge_signal_enabled: bool = True
    edge_extra_cex_enabled: bool = False     # add Kraken+Bitstamp (extra REST; opt-in for hot path)
    edge_promotion_allowed: bool = False
    edge_promotion_min_samples: int = 50
    edge_promotion_min_win_rate: float = 0.80
    # ---- CEX-lead latency edge (grades CEX-implied P(up) vs the MARKET price; PAPER ONLY) ----
    # mode: "shadow" grades only; "gated" may PROPOSE a side on a Wilson-proven bucket (still
    # subject to the deterministic safety floor + execution gate). Default shadow = never trades.
    cex_lead_enabled: bool = True
    cex_lead_mode: str = "shadow"
    cex_lead_min_samples: int = 60
    cex_lead_min_divergence: float = 0.04
    cex_lead_confidence_z: float = 1.64
    cex_lead_min_edge_vs_market: float = 0.0   # required Brier improvement over the market
    cex_lead_tv_strength_thr: float = 0.5      # TradingView strength to count as TV-confirmed
    cex_lead_decisive_thr: float = 0.35        # |cex_p_up-0.5| >= this => late-window move ~decided
    cex_lead_late_ttc_s: float = 90.0          # ttc <= this => late-window convergence-lag zone
    cex_lead_kelly_scale: float = 0.5          # fractional-Kelly size for proven edges
    cex_lead_max_size_frac: float = 2.0        # hard cap on the edge-scaled size multiplier
    # ---- Grok-follow mispricing gate (restrict-only; CEX-lead + edge/TTC alignment) ----
    mispricing_gate_enabled: bool = False
    mispricing_ttc_min_s: float = 180.0
    mispricing_ttc_max_s: float = 240.0
    mispricing_require_confirmed: bool = True
    mispricing_require_stale_down: bool = True
    mispricing_min_executable_margin: float = 0.03
    edge_ttc_gate_enabled: bool = False
    # Tier-1 baseline cohort gate: trade only proven shadow buckets on the quant path.
    baseline_cohort_gate_enabled: bool = True
    baseline_cohort_ttc_min_s: float = 180.0
    baseline_cohort_ttc_max_s: float = 240.0
    baseline_cohort_require_high_edge: bool = True
    baseline_cohort_require_strong_cex: bool = True
    baseline_up_tv_gate_enabled: bool = True
    baseline_down_tv_gate_enabled: bool = True
    baseline_down_block_bullish_range: bool = True
    baseline_down_block_up_strong_bullish: bool = True
    baseline_down_block_volume_active: bool = True
    baseline_down_block_up_strong_range_top: bool = True
    baseline_down_block_bullish_mtf: bool = True
    baseline_down_block_not_stale: bool = True
    baseline_down_block_mid_entry: bool = True
    baseline_down_mid_entry_min: float = 0.55
    baseline_down_mid_entry_max: float = 0.60
    baseline_down_block_single_tf: bool = True
    baseline_down_block_medium_edge: bool = True
    baseline_down_block_bb_expansion_down: bool = True
    # 15m fast lane: scaled TTC band on 15m windows (proven 160-220s cohort → 480-660s).
    baseline_cohort_15m_fast_lane: bool = True
    baseline_cohort_15m_ttc_min_s: float = 160.0
    baseline_cohort_15m_ttc_max_s: float = 220.0
    # 15m DOWN baseline: cohort + MTF only; skip redundant opinion gates (context/tv/down-bias/late).
    green_path_enabled: bool = False
    # When Grok abstains, still follow a Wilson-aligned CEX-lead mispricing stack (not coin-flip explore).
    mispricing_follow_on_abstain: bool = False
    mispricing_follow_size_fraction: float = 0.5
    # ---- directional de-risk ----
    directional_enabled: bool = True            # PULSE_DIRECTIONAL_ENABLED
    # default OFF in code (backward-compatible); enabled via env on the live bot. When on, a
    # directional trade is allowed ONLY in a Wilson-proven-winning bucket (pre-execution block).
    directional_require_winning_bucket: bool = False
    directional_winning_min_samples: int = 30
    # cold-start carve-out: the allowlist would otherwise block EVERY directional trade until a
    # bucket is Wilson-proven-winning, but proving needs trades -> deadlock (bot looks frozen).
    # Allow this capped fraction of otherwise-eligible candidates through as exploration so the bot
    # keeps learning and can DISCOVER winning buckets. 0 = strict block-all; 1 = effectively off.
    directional_explore_rate: float = 0.05
    directional_max_bankroll_frac: float = 0.10   # cap directional open exposure vs starting capital
    correlated_exposure_cap_usd: float = 0.0      # cap same-direction BTC exposure across lanes (0=off)
    directional_down_only: bool = True            # hard block ALL directional UP (no bypass)
    directional_block_up_until_promoted: bool = True  # hard block UP until direction=up promoted
    directional_up_restrictions_enabled: bool = True  # UP-only extra gates (TV/down-bias/RR premium)
    directional_series_slugs: tuple = ()        # empty = all series; else directional only on these
    directional_event_slugs: tuple = ()         # explicit Polymarket event/market slugs (1h quartet)
    directional_hourly_discover: bool = True    # auto-pick BTC/ETH hourly up/down each hour
    directional_15m_discover: bool = True       # auto-pick BTC/ETH 15m up/down each window
    lane_15m_learn_enabled: bool = True         # settled-outcome strategy rewriter for 15m lane
    lane_15m_target_wr: float = 0.60
    lane_15m_kill_wr: float = 0.45
    lane_15m_min_samples: int = 10
    # Shared 15m↔1h cross-horizon learner (restrict/size only; locked — operator approval to redesign)
    cross_horizon_learn_enabled: bool = True
    cross_horizon_min_samples: int = 20
    cross_horizon_target_wr: float = 0.60
    cross_horizon_kill_wr: float = 0.45
    cross_horizon_exploration_rate: float = 0.08
    primary_edge_source: str = "directional"      # report field: directional | none
    # ---- LLM COUNCIL: ensemble of quant + Grok + Claude directional views (PAPER ONLY) ----
    llm_council_enabled: bool = False
    llm_council_min_agreement: float = 0.60
    llm_council_min_margin: float = 0.02
    llm_council_min_members: int = 2
    council_best_ev: bool = False             # pick side by max(prob-ask), not favorite-by-probability
    council_min_executable_margin: float = 0.06  # grok_follow/council: p_win - ask must clear this
    council_tv_member: bool = False           # TV alert direction as a graded council member (follow/fade)
    council_tv_max_age_s: float = 900.0       # per-TF TV read only votes if fresher than this (window clock)
    tv_reset_token: str = ""                  # bump to trigger a one-time reset of tv_reset_members
    tv_reset_members: tuple = ()              # council members to reset when tv_reset_token changes
    claude_decider_enabled: bool = False      # Claude directional second-opinion (council member)
    claude_decider_model: str = ""
    claude_decider_timeout_s: float = 18.0
    # ---- MONTE CARLO pricing ----
    mc_enabled: bool = False
    mc_paths: int = 8000
    mc_scenario_llm: bool = False             # let Grok parameterize directional MC (bounded)
    mc_scenario_claude: bool = False          # optional 2nd MC-scenario source
    # Directional Grok-MC + p_exec self-tune
    dir_mc_enabled: bool = True
    dir_mc_paths: int = 8000
    dir_mc_control_alpha: float = 0.5
    dir_mc_crash_cap: float = 0.25
    p_exec_enabled: bool = True
    p_exec_min_vwap: float = 0.50
    p_exec_explore_rate: float = 0.05
    p_exec_min_promote_n: int = 40
    p_exec_gate_cold: bool = False  # OFF until contexts learn; promote still tracks
    clob_websocket_enabled: bool = True
    stop_min_sharpe: float = 0.0
    stop_sharpe_min_samples: int = 20
    # ---- Osmani 2026 Loop Engineering (3 decoupled lanes + MEMORY.md) ----
    osmani_loop_enabled: bool = False
    osmani_discovery_interval_s: float = 60.0
    osmani_triage_skill_enabled: bool = True
    triage_trend_source: str = "price"       # price | tv — spot trend vs TV UP/DOWN alerts
    grok_trend_source: str = "price"         # price | tv — Grok prompt primary trend read
    price_trend_min_move_bps: float = 2.0    # min bps move from open to call rising/falling
    directional_legacy_tick: bool = False   # when False + osmani on, tick() does not place directional fills
    eth_series_enabled: bool = False            # append ETH 5m/15m slugs when listed
    sizing_promotion_gated: bool = True       # Kelly only on promoted buckets (WS3)
    # ---- Learned Selectivity Gate v1 (between decision and execution; PAPER ONLY) ----
    # Uses live settled-trade bucket evidence to REJECT proven-losing buckets. Can only make the
    # bot MORE selective; never trades/resizes/bypasses the execution gate.
    selectivity_gate_enabled: bool = True
    selectivity_min_samples: int = 50
    selectivity_min_win_rate: float = 0.52
    selectivity_min_profit_factor: float = 0.85
    selectivity_fdr_q: float = 0.10
    selectivity_confidence_z: float = 1.64   # one-sided z for "confidently below breakeven" test
    selectivity_exploration_rate: float = 0.05
    # ---- Learned 1h entry-timing gate (intra-hour bucket; PAPER ONLY) ----
    hourly_entry_gate_enabled: bool = True
    hourly_min_seconds_since_open: float = 900.0   # 15m: TV 15m ladder + 2h review before entry
    hourly_max_seconds_since_open: float = 2700.0  # 45m: no new 1h entries in last 15m of window
    hourly_entry_min_samples: int = 20
    hourly_entry_min_profit_factor: float = 0.85
    hourly_entry_fdr_q: float = 0.10
    hourly_entry_confidence_z: float = 1.64
    hourly_entry_exploration_rate: float = 0.08
    # ---- PRISM ensemble edge (Phase 4; observe-only E/C for the rank R) ----
    prism_enabled: bool = False
    prism_mc_paths: int = 20000
    prism_tv_drift_scale: float = 0.30
    # ---- Directional Tier Engine (regime-aware directional brain; drives paper directional) ----
    tier_engine_enabled: bool = False
    # Phase 1 directional cell learning table (observe-only; grades tier evals + settlements).
    cell_learning_enabled: bool = True
    cell_learning_min_samples: int = 30
    # Phase 2: Wilson cell verdicts nudge tier posterior + size (directional lane only).
    cell_learning_phase2_enabled: bool = False
    # ---- PRISM agents + cross-asset (Phase 6; observe-only; agent gate/sizing default OFF) ----
    prism_agent_gate_enabled: bool = False
    prism_cross_asset_enabled: bool = False
    # ---- PRISM Thompson buckets (Phase 5; learn always, block-gate restrict-only default OFF) ----
    prism_thompson_gate_enabled: bool = False
    prism_bnb_block: bool = False
    # ---- PRISM optimal stopping (Phase 3; restrict-only, legacy directional path; default OFF) ----
    prism_stopping_enabled: bool = False
    # ---- Dynamic pre-trade analysis (synthesize all data before fill; PAPER ONLY) ----
    pre_trade_analysis_enabled: bool = True
    pre_trade_min_score: float = 0.45
    pre_trade_margin_boost_max: float = 0.04
    pre_trade_agreement_boost_max: float = 0.06
    pre_trade_exploration_rate: float = 0.06
    pre_trade_min_size_scale: float = 0.35
    pre_trade_hourly_min_minutes: float = 15.0
    pre_trade_evidence_min_samples: int = 25
    # 1h directional: fresh *_STRONG TV is contrarian veto (ledger: STRONG bleeds, WEAK wins).
    tv_strong_fade_enabled: bool = True
    # Late-window tier SNIPE owns conviction — do not fade aligned STRONG TV on snipes.
    tv_strong_fade_exempt_tier_snipe: bool = True
    calibration_min_samples: int = 30
    calibration_max_shrink: float = 0.5
    # ---- TradingView Context Gate (hard prior, restrict-only; PAPER ONLY) ----
    # Blocks proven-losing entry contexts (TradingView volume spikes, the noise hurst regime, and
    # entries too far from resolution) IMMEDIATELY — before the learned selectivity gate has enough
    # samples. Can only make the bot MORE selective; never trades/resizes/bypasses the execution
    # gate. Default OFF (no behavior change); enabled per-deployment via env.
    tv_context_gate_enabled: bool = False
    tv_context_blocked_volume_states: tuple = ("spike",)
    tv_context_blocked_hurst_regimes: tuple = ("noise",)
    tv_context_max_ttc_s: float = 240.0
    tv_context_block_liquidation_spike: bool = True
    tv_context_block_event_blackout: bool = True
    tv_context_block_grok_event_risk_high: bool = True
    tv_context_exploration_rate: float = 0.0
    # ---- TradingView DOWN-bias gate (Townhall P3; restrict-only) ----
    tv_down_bias_gate_enabled: bool = False
    tv_down_bias_exploration_rate: float = 0.0
    tv_down_bias_block_up_on_bearish_down_stack: bool = True
    tv_down_bias_block_up_tv_down_non_bearish: bool = True
    tv_down_bias_block_up_against_confirmed_down: bool = True
    tv_down_bias_block_mixed_mtf_up: bool = True
    tv_down_bias_block_bullish_supertrend_up: bool = True
    tv_down_bias_block_up_vwap_above: bool = True
    tv_down_bias_block_up_bb_expansion_up: bool = True
    tv_down_bias_block_up_range_breakout_down: bool = True
    tv_down_bias_block_up_range_top: bool = True
    tv_down_bias_block_up_bb_squeeze: bool = True
    tv_down_bias_block_up_markov_chop_noise: bool = True
    tv_down_bias_block_up_htf_bullish: bool = True
    tv_down_bias_block_up_bear_close_near_low: bool = True
    tv_down_bias_block_up_medium_edge: bool = True
    tv_down_bias_block_up_weak_cex: bool = True
    tv_down_bias_block_up_late_ttc: bool = True
    tv_down_bias_block_up_early_ttc: bool = True
    tv_down_bias_block_up_ask_heavy_ob: bool = True
    tv_down_bias_block_up_tf_confirm_conflict: bool = True
    tv_down_bias_block_up_cvd_neutral: bool = True
    tv_down_bias_block_up_cvd_buy_pressure: bool = True
    tv_down_bias_block_up_low_conviction: bool = True
    tv_down_bias_block_up_bearish_mtf_tv_up: bool = True
    tv_down_bias_block_up_mid_ttc: bool = True
    tv_down_bias_block_up_neutral_zscore: bool = True
    tv_down_bias_block_up_medium_confidence: bool = True
    tv_down_bias_block_up_not_stale: bool = True
    tv_down_bias_block_up_volume_active: bool = True
    tv_down_bias_block_up_underdog_entry: bool = True
    tv_down_bias_up_underdog_entry_max: float = 0.55
    tv_down_bias_up_late_ttc_min_s: float = 240.0
    tv_down_bias_up_early_ttc_max_s: float = 120.0
    tv_down_bias_up_mid_ttc_min_s: float = 120.0
    tv_down_bias_up_mid_ttc_max_s: float = 180.0
    tv_down_bias_up_min_conviction: float = 0.40
    tv_mtf_conflict_gate_enabled: bool = True
    tv_mtf_require_confirm: bool = False   # loop arch: conflict veto only, not MTF trade authority
    tv_mtf_require_all_confirm: bool = False  # require all MTF TFs (e.g. 2/3/4) agree on direction
    tv_mtf_require_side_align: bool = False
    tv_mtf_conflict_exploration_rate: float = 0.0
    # ---- verifiable stop conditions (agent-independent kill switches; Loop Eng #6) ----
    stop_enabled: bool = True
    stop_rolling_n: int = 50
    stop_min_samples: int = 30
    stop_min_profit_factor: float = 0.85
    stop_max_drawdown_pct: float = 25.0
    # ---- Late-window high-conviction entry mode (time-decay edge; PAPER ONLY) ----
    # When enabled, only late-window AND high-conviction setups may trade (restrict-only). The edge
    # is ALWAYS measured observe-only (cohort vs other) so it can be graded before being enabled.
    late_window_entry_enabled: bool = False
    late_window_max_ttc_s: float = 120.0
    late_window_min_conviction: float = 0.40
    signal_engine_enabled: bool = True       # OBSERVE-ONLY Simons-style raw signals (never trade)
    factor_model_enabled: bool = True        # OBSERVE-ONLY BTC-pulse factor/context model
    markov_enabled: bool = True              # OBSERVE-ONLY Markov regime machine
    edge_model_enabled: bool = True          # OBSERVE-ONLY calibrated edge model (no authority)
    # ---- closed-loop learning: blend the calibrated edge model into the DIRECTIONAL decision ----
    # The bot's own settled-trade experience (online logistic edge model) adjusts P(up) used by
    # decide(). Influence is EARNED (ramps with sample count), GATED (only when calibrated), and
    # SELF-DISABLING (drops to 0 if calibration error exceeds the cap). The strict execution gate,
    # paper-realism, and ledger reconciliation are UNTOUCHED — learning can never bypass them, and
    # this is PAPER ONLY. Default OFF (no behavior change); enabled per-deployment via env.
    learning_enabled: bool = False
    learning_min_samples: int = 60           # min settled labels before any influence
    learning_max_weight: float = 0.5         # cap on the model's weight in the blend (<=0.5)
    learning_ramp_samples: float = 300.0     # labels over which weight ramps 0 -> max
    learning_max_calib_error: float = 0.15   # disable influence if ECE worse than this
    learning_bench_min_samples: int = 20     # graded windows before the market-beating gate applies
    learning_bench_margin: float = 0.0       # model Brier must beat market Brier by >= this to blend
    sizing_enabled: bool = False             # paper Kelly sizing: default OFF (size unchanged)
    sizing_hard_cap_usd: float = 10.0
    sizing_daily_loss_cap_usd: float = 50.0
    sizing_bankroll_usd: float = 1000.0
    # Osmani lane: decide bet size from half-Kelly × pre-trade readiness (PAPER ONLY).
    osmani_autonomous_sizing: bool = True
    osmani_sizing_min_usd: float = 1.0
    # Evidence-based High-WR scalar auto-tune (min_edge / min_entry / exec EV / hourly SSO / sweet).
    gate_auto_tune_enabled: bool = True
    gate_auto_tune_lookback_n: int = 24
    gate_auto_tune_min_samples: int = 12
    gate_auto_tune_target_wr: float = 0.65
    gate_auto_tune_kill_wr: float = 0.50
    gate_auto_tune_starve_fph: float = 0.8
    gate_auto_tune_rich_fph: float = 3.0
    gate_auto_tune_cooldown: int = 6
    # notional starting capital for the on-hand-capital display (paper). on_hand = start + realized.
    starting_capital_usd: float = 500.0
    # ---- TradingView indicator webhook intake (OBSERVE-ONLY external signal) ----
    # Enabled only when a shared secret is set. Bound to 127.0.0.1 by default (private to host);
    # alerts are candidate signals only — they can never place/resize/bypass a paper trade.
    tradingview_secret: str = ""
    tradingview_allowed_symbols: tuple = ("BTCUSD", "INDEX:BTCUSD", "BTC/USD", "BTC", "XBTUSD")
    tradingview_bot_name: str = "hermes"
    tradingview_event_id_suffix: str = ""
    tradingview_webhook_host: str = "127.0.0.1"
    tradingview_webhook_port: int = 8787
    tradingview_webhook_path: str = "/webhooks/tradingview"
    tradingview_max_age_s: float = 90.0
    tradingview_feature_symbol: str = "BTCUSD"   # TV INDEX:BTCUSD — 5m/10m/15m MTF
    tradingview_mtf_timeframes: tuple = ("5", "10", "15", "20", "25", "30", "35", "40", "45", "50", "55", "60")
    tradingview_drop_timeframes: tuple = ()      # retired chart TFs: not tracked per-TF (no council/dash)
    tradingview_allowed_bot_names: tuple = ()    # bot_name allow-list for TV alerts (default {bot_name})
    tradingview_mtf_confirm_window_s: float = 360.0
    tradingview_mtf_confirm_window_10m_s: float = 660.0
    tradingview_mtf_confirm_window_15m_s: float = 2250.0
    tradingview_mtf_confirm_window_2m_s: float = 300.0
    tradingview_mtf_confirm_window_3m_s: float = 1200.0
    tradingview_mtf_confirm_window_4m_s: float = 1500.0
    tradingview_mtf_confirm_window_5m_s: float = 1500.0
    tradingview_mtf_confirm_window_13m_s: float = 840.0
    tradingview_mtf_confirm_window_30m_s: float = 4500.0
    tradingview_mtf_confirm_window_45m_s: float = 6750.0
    tradingview_mtf_confirm_window_55m_s: float = 8250.0
    # Rolling FIFO of last N TV alerts per symbol for Grok price-path trend (hard cap 50).
    tradingview_alert_history_per_symbol: int = 50
    # Short-term chart lean: last 6–12 of those 50 (5m bars) drives trade bias.
    tv_15m_short_path_n: int = 8  # alias used by 15m lane (= 5m short path)
    tv_15m_chart_lean_enabled: bool = True
    tv_15m_chart_lean_size: bool = True
    # 1h lane: last 12 × 5m bar-close (~1h) drives entry lean; hard gate when opposed.
    tv_1h_short_path_n: int = 12
    tv_1h_chart_lean_enabled: bool = True
    tv_1h_chart_lean_gate: bool = True
    tv_1h_chart_lean_size: bool = True
    # RSI divergence overlay (separate FIFO; soft confirm/fade).
    tradingview_rsi_div_history_per_symbol: int = 20
    tv_rsi_overlay_enabled: bool = True
    tv_rsi_overlay_size: bool = True
    tv_rsi_overlay_max_age_s: float = 2700.0
    tv_rsi_overlay_aligned_mult: float = 1.15
    tv_rsi_overlay_opposed_mult: float = 0.45
    # Binary Intel — invented quant + Grok pre/post-trade scripts (PAPER ONLY).
    binary_intel_enabled: bool = True
    binary_intel_grok_compute: bool = True
    binary_intel_min_score: float = 0.28
    binary_intel_exploration_rate: float = 0.05
    binary_intel_min_size_scale: float = 0.40
    binary_intel_kelly_fraction: float = 0.25
    # SAWR — Self-Adjusting Win-Rate meta-controller (Fill-Quality Pareto + Beta affinity).
    sawr_enabled: bool = True
    sawr_lookback_n: int = 40
    sawr_min_samples: int = 8
    sawr_target_wr: float = 0.60
    sawr_kill_wr: float = 0.48
    sawr_starve_fph: float = 0.6
    sawr_rich_fph: float = 4.0
    sawr_wr_weight: float = 1.0
    sawr_fill_weight: float = 0.35
    sawr_kill_penalty: float = 2.0
    sawr_cooldown: int = 5
    # CHRONOS — pre-decision walk-forward dry-run before size/trade authority.
    chronos_enabled: bool = True
    chronos_min_cohort_n: int = 4
    chronos_proceed_cvs: float = 0.05
    chronos_exploration_rate: float = 0.12
    chronos_kill_wr: float = 0.48
    # RSI 30/70 band heartbeats (separate FIFO; Grok + MC context).
    tradingview_rsi_band_history_per_symbol: int = 50
    tv_rsi_band_enabled: bool = True
    tv_rsi_band_max_age_s: float = 900.0
    tv_rsi_divergence_analysis_enabled: bool = True
    # 2h TV trend review (observe-only by default; pretrade/council flags default OFF).
    tv_2h_review_enabled: bool = True
    tv_2h_lookback_s: float = 7200.0
    tv_2h_review_pretrade: bool = False
    tv_2h_council_grade: bool = False
    tv_2h_alert_history_cap: int = 50
    # Polymarket series to trade (default 15m only; set PULSE_SERIES_SLUGS for multi-series).
    pulse_series_slugs: tuple = (SERIES_SLUG_15M,)
    tradingview_signal_max_feature_age_s: float = 3600.0  # match triage TV max age (hourly)
    # TradingView as the DIRECTIONAL INDICATION SIGNAL (restrict-only): when on, a paper trade is
    # only taken if a FRESH TradingView signal exists and its direction matches the trade side. It
    # can only PREVENT trades (never force one or bypass the execution gate). Default OFF.
    tradingview_signal_gate_enabled: bool = False
    tradingview_min_signal_strength: float = 0.0   # 0=off; e.g. 0.72 blocks WEAK, keeps STRONG
    # TV confidence tier: observe-only min_edge/max_price modulation at 15m sweet spot (not a gate).
    tv_confidence_tier_enabled: bool = True
    tv_tier_require_sweet_spot: bool = True
    tv_tier_15m_only: bool = True
    tv_tier_aligned_strength_min: float = 0.72
    tv_tier_a_min_edge_delta: float = -0.005
    tv_tier_a_max_price_delta: float = 0.02
    tv_tier_c_min_edge_delta: float = 0.005
    tv_tier_c_max_price_delta: float = -0.03
    # forward-return horizon (s): for EVERY TradingView signal, the bot snapshots the oracle BTC
    # price and re-checks it this many seconds later to learn whether the signal predicted the
    # move — building a prediction from the history of ALL signals (traded or not). Observe-only.
    tradingview_signal_horizon_s: float = 300.0
    # TradingView signal-bucket PROMOTION diagnostics (observe-only by default). A bucket is only
    # flagged eligible if win_rate >= min_win_rate, EV-after-slippage > 0, clean reconciliation,
    # and >= min_samples. Promotion to trading authority requires this flag AND explicit wiring.
    tradingview_promotion_allowed: bool = False
    tradingview_promotion_min_samples: int = 50
    tradingview_promotion_min_win_rate: float = 0.80
    data_dir: str = "/data"

    @classmethod
    def from_env(cls) -> "PulseConfig":
        from engine.pulse.tradingview import normalize_symbol
        from engine.pulse.markets import SERIES_SLUG_ETH_5M, SERIES_SLUG_ETH_15M
        _series_slugs = tuple(
            s.strip() for s in os.getenv(
                "PULSE_SERIES_SLUGS",
                "btc-up-or-down-hourly,eth-up-or-down-hourly").split(",") if s.strip())
        _dir_series = tuple(
            s.strip() for s in os.getenv(
                "PULSE_DIRECTIONAL_SERIES_SLUGS",
                "btc-up-or-down-hourly,eth-up-or-down-hourly").split(",") if s.strip())
        _dir_events = tuple(
            s.strip() for s in os.getenv("PULSE_DIRECTIONAL_EVENT_SLUGS", "").split(",")
            if s.strip())
        if str(os.getenv("PULSE_ETH_SERIES_ENABLED", "0")).strip().lower() in (
                "1", "true", "yes", "on"):
            _series_slugs = tuple(dict.fromkeys(
                _series_slugs + (SERIES_SLUG_ETH_5M, SERIES_SLUG_ETH_15M)))
        _grok_mode = (os.getenv("PULSE_GROK_DECIDER_MODE", "shadow") or "shadow").strip().lower()
        if _grok_mode == "follow":
            _grok_mode = "shadow"
        return cls(
            tick_seconds=_envf("PULSE_TICK_SECONDS", 4.0),
            size_usd=_envf("PULSE_SIZE_USD", 5.0),
            min_edge=_envf("PULSE_MIN_EDGE", 0.03),
            min_seconds_to_close=_envf("PULSE_MIN_SECONDS_TO_CLOSE", 4.0),
            min_depth_usd=_envf("PULSE_MIN_DEPTH_USD", 1.0),
            edge_buffer=_envf("PULSE_EDGE_BUFFER", 0.01),
            max_price=_envf("PULSE_MAX_PRICE", 0.97),
            min_reward_risk=_envf("PULSE_MIN_REWARD_RISK", 0.0),
            min_reward_risk_up_premium=_envf("PULSE_MIN_REWARD_RISK_UP_PREMIUM", 0.15),
            max_open_lag_s=_envf("PULSE_MAX_OPEN_LAG_S", 20.0),
            max_open_lag_15m_s=_envf("PULSE_MAX_OPEN_LAG_15M_S", 240.0),
            vol_window_s=_envf("PULSE_VOL_WINDOW_S", 900.0),
            settle_grace_s=_envf("PULSE_SETTLE_GRACE_S", 180.0),
            fresh_start=str(os.getenv("PULSE_FRESH_START", "")).strip().lower()
            in ("1", "true", "yes", "on"),
            min_seconds_since_open=_envf("PULSE_MIN_SECONDS_SINCE_OPEN", 30.0),
            min_vol_samples=int(_envf("PULSE_MIN_VOL_SAMPLES", 12)),
            sigma_trust_floor=_envf("PULSE_SIGMA_TRUST_FLOOR", 2.0e-6),
            basis_buffer=_envf("PULSE_BASIS_BUFFER", 0.02),
            grok_overlay_enabled=str(os.getenv("GROK_OVERLAY_ENABLED", "")).strip().lower()
            in ("1", "true", "yes", "on"),
            grok_overlay_interval_s=_envf("GROK_OVERLAY_INTERVAL_S", 180.0),
            grok_overlay_max_calls_per_hour=int(_envf("GROK_OVERLAY_MAX_CALLS_PER_HOUR", 20)),
            grok_signal_analyst_enabled=str(os.getenv("GROK_SIGNAL_ANALYST_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_signal_predictor_enabled=str(os.getenv("GROK_SIGNAL_PREDICTOR_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_analyst_interval_s=_envf("GROK_ANALYST_INTERVAL_S", 1800.0),
            grok_budget_daily_usd=_envf("GROK_BUDGET_DAILY_USD", 50.0),
            grok_est_usd_per_call=_envf("GROK_EST_USD_PER_CALL", 0.02),
            grok_predictor_max_calls_per_hour=int(_envf("GROK_PREDICTOR_MAX_CALLS_PER_HOUR", 90)),
            grok_analyst_max_calls_per_hour=int(_envf("GROK_ANALYST_MAX_CALLS_PER_HOUR", 8)),
            grok_decider_mode=_grok_mode,
            grok_decider_model=(os.getenv("PULSE_GROK_DECIDER_MODEL", "grok-4.3")
                                or "grok-4.3").strip(),
            grok_decider_timeout_s=_envf("PULSE_GROK_DECIDER_TIMEOUT_S", 12.0),
            grok_decider_use_search=str(os.getenv("PULSE_GROK_DECIDER_USE_SEARCH", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_decider_min_confidence=_envf("PULSE_GROK_DECIDER_MIN_CONFIDENCE", 0.55),
            grok_decider_ttl_s=_envf("PULSE_GROK_DECIDER_TTL_S", 240.0),
            grok_decider_max_calls_per_hour=int(_envf("PULSE_GROK_DECIDER_MAX_CALLS_PER_HOUR", 200)),
            grok_tiered_compute_enabled=str(os.getenv("PULSE_GROK_TIERED_COMPUTE", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_tier_full_divergence_min=_envf("PULSE_GROK_TIER_FULL_DIVERGENCE_MIN", 0.025),
            grok_tier_deep_divergence_min=_envf("PULSE_GROK_TIER_DEEP_DIVERGENCE_MIN", 0.04),
            grok_decider_explore_rate=_envf("PULSE_GROK_DECIDER_EXPLORE_RATE", 0.0),
            grok_decider_explore_size_fraction=_envf("PULSE_GROK_DECIDER_EXPLORE_SIZE_FRACTION", 0.5),
            grok_decider_explore_min_view_margin=_envf(
                "PULSE_GROK_DECIDER_EXPLORE_MIN_VIEW_MARGIN", 0.08),
            grok_up_min_p_win=_envf("PULSE_GROK_UP_MIN_P_WIN", 0.58),
            grok_decider_adaptive=str(os.getenv("PULSE_GROK_DECIDER_ADAPTIVE", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            verifier_enabled=str(os.getenv("PULSE_VERIFIER_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            verifier_fail_open=str(os.getenv("PULSE_VERIFIER_FAIL_OPEN", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            verifier_follow_require_verdict=str(os.getenv("PULSE_VERIFIER_FOLLOW_REQUIRE_VERDICT", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            verifier_explore_approve=str(os.getenv("PULSE_VERIFIER_EXPLORE_APPROVE", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            verifier_explore_max_size_fraction=_envf(
                "PULSE_VERIFIER_EXPLORE_MAX_SIZE_FRACTION", 0.5),
            verifier_max_calls_per_hour=int(_envf("PULSE_VERIFIER_MAX_CALLS_PER_HOUR", 120)),
            research_loop_enabled=str(os.getenv("PULSE_RESEARCH_LOOP_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            research_interval_s=_envf("PULSE_RESEARCH_INTERVAL_S", 1800.0),
            research_event_min_gap_s=_envf("PULSE_RESEARCH_EVENT_MIN_GAP_S", 600.0),
            research_avoid_max=int(_envf("PULSE_RESEARCH_AVOID_MAX", 14)),
            research_exploit_max=int(_envf("PULSE_RESEARCH_EXPLOIT_MAX", 10)),
            lessons_revalidate_ttl_s=_envf("PULSE_LESSONS_REVALIDATE_TTL_S", 21600.0),
            research_exploit_size_mult=_envf("PULSE_RESEARCH_EXPLOIT_SIZE_MULT", 1.5),
            research_auto_apply=str(os.getenv("PULSE_RESEARCH_AUTO_APPLY", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            research_forbid_size_increase=str(
                os.getenv("PULSE_RESEARCH_FORBID_SIZE_INCREASE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            research_max_calls_per_hour=int(_envf("PULSE_RESEARCH_MAX_CALLS_PER_HOUR", 6)),
            claude_budget_daily_usd=_envf("CLAUDE_BUDGET_DAILY_USD", 10.0),
            claude_est_usd_per_call=_envf("CLAUDE_EST_USD_PER_CALL", 0.01),
            grok_news_enabled=str(os.getenv("PULSE_GROK_NEWS_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_news_refresh_s=_envf("PULSE_GROK_NEWS_REFRESH_S", 300.0),
            price_source=(os.getenv("PULSE_PRICE_SOURCE", "auto") or "auto").strip().lower(),
            price_sampler_interval_s=_envf("PULSE_PRICE_SAMPLER_INTERVAL_S", 1.0),
            oracle_feed_type=(os.getenv("HERMES_ORACLE_FEED_TYPE",
                                        "chainlink_data_streams_refprice") or "").strip().lower(),
            oracle_symbol=(os.getenv("HERMES_ORACLE_SYMBOL", "btc/usd") or "btc/usd").strip().lower(),
            fast_feeds=tuple(s.strip().lower() for s in os.getenv(
                "HERMES_FAST_FEEDS", "binance_btcusdt,coinbase_btcusd").split(",") if s.strip()),
            settlement_source_priority=tuple(s.strip().lower() for s in os.getenv(
                "HERMES_SETTLEMENT_SOURCE_PRIORITY",
                "polymarket_resolution").split(",") if s.strip()),
            proxy_max_close_lag_s=_envf("HERMES_PROXY_MAX_CLOSE_LAG_S", 30.0),
            rtds_max_age_s=_envf("PULSE_RTDS_MAX_AGE_S", 45.0),
            price_max_age_s=_envf("PULSE_PRICE_MAX_AGE_S", 60.0),
            rtds_enabled=str(os.getenv("HERMES_RTDS_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            exec_max_spread=_envf("PULSE_EXEC_MAX_SPREAD", 0.06),
            exec_min_order_usd=_envf("PULSE_EXEC_MIN_ORDER_USD", 1.0),
            exec_max_depth_consume_frac=_envf("PULSE_EXEC_MAX_DEPTH_CONSUME_FRAC", 0.5),
            exec_min_ev_after_slippage=_envf("PULSE_EXEC_MIN_EV", 0.02),
            min_entry_price=_envf("PULSE_MIN_ENTRY_PRICE", 0.50),
            exec_max_book_age_s=_envf("PULSE_EXEC_MAX_BOOK_AGE_S", 30.0),
            research_features_enabled=str(os.getenv("HERMES_RESEARCH_FEATURES_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_signal_enabled=str(os.getenv("HERMES_EDGE_SIGNAL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_extra_cex_enabled=str(os.getenv("HERMES_EDGE_EXTRA_CEX_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_promotion_allowed=str(os.getenv("HERMES_EDGE_PROMOTION_ALLOWED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_promotion_min_samples=int(_envf("HERMES_EDGE_PROMOTION_MIN_SAMPLES", 50)),
            edge_promotion_min_win_rate=_envf("HERMES_EDGE_PROMOTION_MIN_WIN_RATE", 0.80),
            cex_lead_enabled=str(os.getenv("PULSE_CEX_LEAD_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            cex_lead_mode=str(os.getenv("PULSE_CEX_LEAD_MODE", "shadow")).strip().lower(),
            cex_lead_min_samples=int(_envf("PULSE_CEX_LEAD_MIN_SAMPLES", 60)),
            cex_lead_min_divergence=_envf("PULSE_CEX_LEAD_MIN_DIVERGENCE", 0.04),
            cex_lead_confidence_z=_envf("PULSE_CEX_LEAD_CONFIDENCE_Z", 1.64),
            cex_lead_min_edge_vs_market=_envf("PULSE_CEX_LEAD_MIN_EDGE_VS_MARKET", 0.0),
            cex_lead_tv_strength_thr=_envf("PULSE_CEX_LEAD_TV_STRENGTH_THR", 0.5),
            cex_lead_decisive_thr=_envf("PULSE_CEX_LEAD_DECISIVE_THR", 0.35),
            cex_lead_late_ttc_s=_envf("PULSE_CEX_LEAD_LATE_TTC_S", 90.0),
            cex_lead_kelly_scale=_envf("PULSE_CEX_LEAD_KELLY_SCALE", 0.5),
            cex_lead_max_size_frac=_envf("PULSE_CEX_LEAD_MAX_SIZE_FRAC", 2.0),
            mispricing_gate_enabled=str(os.getenv("PULSE_MISPRICING_GATE_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            mispricing_ttc_min_s=_envf("PULSE_MISPRICING_TTC_MIN_S", 180.0),
            mispricing_ttc_max_s=_envf("PULSE_MISPRICING_TTC_MAX_S", 240.0),
            mispricing_require_confirmed=str(
                os.getenv("PULSE_MISPRICING_REQUIRE_CONFIRMED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            mispricing_require_stale_down=str(
                os.getenv("PULSE_MISPRICING_REQUIRE_STALE_DOWN", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            mispricing_min_executable_margin=_envf(
                "PULSE_MISPRICING_MIN_EXECUTABLE_MARGIN", 0.03),
            edge_ttc_gate_enabled=str(os.getenv("PULSE_EDGE_TTC_GATE_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            baseline_cohort_gate_enabled=str(
                os.getenv("PULSE_BASELINE_COHORT_GATE_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_cohort_ttc_min_s=_envf("PULSE_BASELINE_COHORT_TTC_MIN_S", 180.0),
            baseline_cohort_ttc_max_s=_envf("PULSE_BASELINE_COHORT_TTC_MAX_S", 240.0),
            baseline_cohort_require_high_edge=str(
                os.getenv("PULSE_BASELINE_COHORT_REQUIRE_HIGH_EDGE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_cohort_require_strong_cex=str(
                os.getenv("PULSE_BASELINE_COHORT_REQUIRE_STRONG_CEX", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_up_tv_gate_enabled=str(
                os.getenv("PULSE_BASELINE_UP_TV_GATE_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_tv_gate_enabled=str(
                os.getenv("PULSE_BASELINE_DOWN_TV_GATE_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_bullish_range=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_BULLISH_RANGE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_up_strong_bullish=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_UP_STRONG_BULLISH", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_volume_active=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_VOLUME_ACTIVE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_up_strong_range_top=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_UP_STRONG_RANGE_TOP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_bullish_mtf=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_BULLISH_MTF", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_not_stale=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_NOT_STALE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_mid_entry=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_MID_ENTRY", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_mid_entry_min=_envf("PULSE_BASELINE_DOWN_MID_ENTRY_MIN", 0.55),
            baseline_down_mid_entry_max=_envf("PULSE_BASELINE_DOWN_MID_ENTRY_MAX", 0.60),
            baseline_down_block_single_tf=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_SINGLE_TF", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_medium_edge=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_MEDIUM_EDGE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_bb_expansion_down=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_BB_EXPANSION_DOWN", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_cohort_15m_fast_lane=str(
                os.getenv("PULSE_BASELINE_COHORT_15M_FAST_LANE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_cohort_15m_ttc_min_s=_envf("PULSE_BASELINE_COHORT_15M_TTC_MIN_S", 160.0),
            baseline_cohort_15m_ttc_max_s=_envf("PULSE_BASELINE_COHORT_15M_TTC_MAX_S", 220.0),
            green_path_enabled=str(os.getenv("PULSE_GREEN_PATH_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            mispricing_follow_on_abstain=str(
                os.getenv("PULSE_MISPRICING_FOLLOW_ON_ABSTAIN", "0")).strip().lower()
            in ("1", "true", "yes", "on"),
            mispricing_follow_size_fraction=_envf("PULSE_MISPRICING_FOLLOW_SIZE_FRACTION", 0.5),
            directional_enabled=str(os.getenv("PULSE_DIRECTIONAL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            directional_require_winning_bucket=str(os.getenv("PULSE_DIRECTIONAL_REQUIRE_WINNING", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            directional_winning_min_samples=int(_envf("PULSE_DIRECTIONAL_WINNING_MIN_SAMPLES", 30)),
            directional_explore_rate=_envf("PULSE_DIRECTIONAL_EXPLORE_RATE", 0.05),
            directional_max_bankroll_frac=_envf("PULSE_DIRECTIONAL_MAX_BANKROLL_FRAC", 0.10),
            correlated_exposure_cap_usd=_envf("PULSE_CORRELATED_EXPOSURE_CAP_USD", 0.0),
            directional_down_only=str(
                os.getenv("PULSE_DIRECTIONAL_DOWN_ONLY", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            directional_block_up_until_promoted=str(
                os.getenv("PULSE_DIRECTIONAL_BLOCK_UP_UNTIL_PROMOTED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            directional_up_restrictions_enabled=str(
                os.getenv("PULSE_DIRECTIONAL_UP_RESTRICTIONS_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            directional_series_slugs=_dir_series,
            directional_event_slugs=_dir_events,
            directional_hourly_discover=str(
                os.getenv("PULSE_DIRECTIONAL_HOURLY_DISCOVER", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            directional_15m_discover=str(
                os.getenv("PULSE_DIRECTIONAL_15M_DISCOVER", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            lane_15m_learn_enabled=str(
                os.getenv("PULSE_LANE_15M_LEARN_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            lane_15m_target_wr=_envf("PULSE_LANE_15M_TARGET_WR", 0.60),
            lane_15m_kill_wr=_envf("PULSE_LANE_15M_KILL_WR", 0.45),
            lane_15m_min_samples=int(_envf("PULSE_LANE_15M_MIN_SAMPLES", 10)),
            cross_horizon_learn_enabled=str(
                os.getenv("PULSE_CROSS_HORIZON_LEARN_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            cross_horizon_min_samples=int(_envf("PULSE_CROSS_HORIZON_MIN_SAMPLES", 20)),
            cross_horizon_target_wr=_envf("PULSE_CROSS_HORIZON_TARGET_WR", 0.60),
            cross_horizon_kill_wr=_envf("PULSE_CROSS_HORIZON_KILL_WR", 0.45),
            cross_horizon_exploration_rate=_envf("PULSE_CROSS_HORIZON_EXPLORATION_RATE", 0.08),
            primary_edge_source=str(os.getenv("PULSE_PRIMARY_EDGE_SOURCE", "directional")).strip()
            or "directional",
            llm_council_enabled=str(os.getenv("PULSE_LLM_COUNCIL_ENABLED", "0")).strip().lower()
            in ("1", "true", "yes", "on"),
            llm_council_min_agreement=_envf("PULSE_LLM_COUNCIL_MIN_AGREEMENT", 0.60),
            llm_council_min_margin=_envf("PULSE_LLM_COUNCIL_MIN_MARGIN", 0.02),
            llm_council_min_members=int(_envf("PULSE_LLM_COUNCIL_MIN_MEMBERS", 2)),
            council_best_ev=str(os.getenv("PULSE_COUNCIL_BEST_EV", "0")).strip().lower()
            in ("1", "true", "yes", "on"),
            council_min_executable_margin=_envf(
                "PULSE_COUNCIL_MIN_EXECUTABLE_MARGIN", 0.06),
            council_tv_member=str(os.getenv("PULSE_COUNCIL_TV_MEMBER", "0")).strip().lower()
            in ("1", "true", "yes", "on"),
            council_tv_max_age_s=_envf("PULSE_TV_COUNCIL_MAX_AGE_S", 900.0),
            tv_reset_token=(os.getenv("PULSE_TV_RESET_TOKEN", "") or "").strip(),
            tv_reset_members=tuple(
                s.strip() for s in (os.getenv("PULSE_TV_RESET_MEMBERS", "") or "").split(",")
                if s.strip()),
            claude_decider_enabled=str(os.getenv("PULSE_CLAUDE_DECIDER_ENABLED", "0")).strip().lower()
            in ("1", "true", "yes", "on"),
            claude_decider_model=str(os.getenv("PULSE_CLAUDE_DECIDER_MODEL", "") or ""),
            claude_decider_timeout_s=_envf("PULSE_CLAUDE_DECIDER_TIMEOUT_S", 18.0),
            mc_enabled=str(os.getenv("PULSE_MC_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            mc_paths=int(_envf("PULSE_MC_PATHS", 8000)),
            mc_scenario_llm=str(os.getenv("PULSE_MC_SCENARIO_LLM", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            mc_scenario_claude=str(os.getenv("PULSE_MC_SCENARIO_CLAUDE", "0")).strip().lower()
            in ("1", "true", "yes", "on"),
            dir_mc_enabled=str(os.getenv("PULSE_DIR_MC_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            dir_mc_paths=int(_envf("PULSE_DIR_MC_PATHS", 8000)),
            dir_mc_control_alpha=_envf("PULSE_DIR_MC_CONTROL_ALPHA", 0.5),
            dir_mc_crash_cap=_envf("PULSE_DIR_MC_CRASH_CAP", 0.25),
            p_exec_enabled=str(os.getenv("PULSE_P_EXEC_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            p_exec_min_vwap=_envf("PULSE_P_EXEC_MIN_VWAP", 0.50),
            p_exec_explore_rate=_envf("PULSE_P_EXEC_EXPLORE_RATE", 0.05),
            p_exec_min_promote_n=int(_envf("PULSE_P_EXEC_MIN_PROMOTE_N", 40)),
            p_exec_gate_cold=str(os.getenv("PULSE_P_EXEC_GATE_COLD", "0")).strip().lower()
            in ("1", "true", "yes", "on"),
            clob_websocket_enabled=str(os.getenv("PULSE_CLOB_WEBSOCKET_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            stop_min_sharpe=_envf("PULSE_STOP_MIN_SHARPE", 0.0),
            stop_sharpe_min_samples=int(_envf("PULSE_STOP_SHARPE_MIN_SAMPLES", 20)),
            eth_series_enabled=str(os.getenv("PULSE_ETH_SERIES_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            osmani_loop_enabled=str(os.getenv("PULSE_OSMANI_LOOP_ENABLED", "1"))
            .lower() in ("1", "true", "yes"),
            osmani_discovery_interval_s=_envf("PULSE_OSMANI_DISCOVERY_INTERVAL_S", 60.0),
            osmani_triage_skill_enabled=str(os.getenv("PULSE_OSMANI_TRIAGE_SKILL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes"),
            triage_trend_source=(os.getenv("PULSE_TRIAGE_TREND_SOURCE", "price") or "price").strip().lower(),
            grok_trend_source=(os.getenv("PULSE_GROK_TREND_SOURCE", "price") or "price").strip().lower(),
            price_trend_min_move_bps=_envf("PULSE_PRICE_TREND_MIN_MOVE_BPS", 2.0),
            directional_legacy_tick=str(os.getenv("PULSE_DIRECTIONAL_LEGACY_TICK", "0"))
            .strip().lower() in ("1", "true", "yes"),
            sizing_promotion_gated=str(os.getenv("PULSE_SIZING_PROMOTION_GATED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            selectivity_gate_enabled=str(os.getenv("PULSE_SELECTIVITY_GATE_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            selectivity_min_samples=int(_envf("PULSE_SELECTIVITY_MIN_SAMPLES", 50)),
            selectivity_min_win_rate=_envf("PULSE_SELECTIVITY_MIN_WIN_RATE", 0.52),
            selectivity_min_profit_factor=_envf("PULSE_SELECTIVITY_MIN_PROFIT_FACTOR", 0.85),
            selectivity_fdr_q=_envf("PULSE_SELECTIVITY_FDR_Q", 0.10),
            selectivity_confidence_z=_envf("PULSE_SELECTIVITY_CONFIDENCE_Z", 1.64),
            selectivity_exploration_rate=_envf("PULSE_SELECTIVITY_EXPLORATION_RATE", 0.05),
            hourly_entry_gate_enabled=str(os.getenv("PULSE_HOURLY_ENTRY_GATE_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            hourly_min_seconds_since_open=_envf("PULSE_HOURLY_MIN_SECONDS_SINCE_OPEN", 900.0),
            hourly_max_seconds_since_open=_envf("PULSE_HOURLY_MAX_SECONDS_SINCE_OPEN", 2700.0),
            hourly_entry_min_samples=int(_envf("PULSE_HOURLY_ENTRY_MIN_SAMPLES", 20)),
            hourly_entry_min_profit_factor=_envf("PULSE_HOURLY_ENTRY_MIN_PROFIT_FACTOR", 0.85),
            hourly_entry_fdr_q=_envf("PULSE_HOURLY_ENTRY_FDR_Q", 0.10),
            hourly_entry_confidence_z=_envf("PULSE_HOURLY_ENTRY_CONFIDENCE_Z", 1.64),
            hourly_entry_exploration_rate=_envf("PULSE_HOURLY_ENTRY_EXPLORATION_RATE", 0.08),
            prism_enabled=str(os.getenv("PULSE_PRISM_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            prism_mc_paths=int(_envf("PULSE_PRISM_MC_PATHS", 20000)),
            prism_tv_drift_scale=_envf("PULSE_PRISM_TV_DRIFT_SCALE", 0.30),
            tier_engine_enabled=str(os.getenv("PULSE_TIER_ENGINE_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            cell_learning_enabled=str(os.getenv("PULSE_CELL_LEARNING_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            cell_learning_min_samples=int(_envf("PULSE_CELL_LEARNING_MIN_SAMPLES", 30)),
            cell_learning_phase2_enabled=str(os.getenv("PULSE_CELL_LEARNING_PHASE2_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            prism_agent_gate_enabled=str(os.getenv("PULSE_PRISM_AGENT_GATE_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            prism_cross_asset_enabled=str(os.getenv("PULSE_PRISM_CROSS_ASSET", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            prism_thompson_gate_enabled=str(os.getenv("PULSE_PRISM_THOMPSON_GATE_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            prism_bnb_block=str(os.getenv("PULSE_PRISM_BNB_BLOCK", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            prism_stopping_enabled=str(os.getenv("PULSE_PRISM_STOPPING_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            pre_trade_analysis_enabled=str(os.getenv("PULSE_PRE_TRADE_ANALYSIS_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            pre_trade_min_score=_envf("PULSE_PRE_TRADE_MIN_SCORE", 0.45),
            pre_trade_margin_boost_max=_envf("PULSE_PRE_TRADE_MARGIN_BOOST_MAX", 0.04),
            pre_trade_agreement_boost_max=_envf("PULSE_PRE_TRADE_AGREEMENT_BOOST_MAX", 0.06),
            pre_trade_exploration_rate=_envf("PULSE_PRE_TRADE_EXPLORATION_RATE", 0.06),
            pre_trade_min_size_scale=_envf("PULSE_PRE_TRADE_MIN_SIZE_SCALE", 0.35),
            pre_trade_hourly_min_minutes=_envf("PULSE_PRE_TRADE_HOURLY_MIN_MINUTES", 15.0),
            pre_trade_evidence_min_samples=int(_envf("PULSE_PRE_TRADE_EVIDENCE_MIN_SAMPLES", 25)),
            tv_strong_fade_enabled=str(os.getenv("PULSE_TV_STRONG_FADE_ENABLED", "1"))
                .strip().lower() in ("1", "true", "yes", "on"),
            tv_strong_fade_exempt_tier_snipe=str(
                os.getenv("PULSE_TV_STRONG_FADE_EXEMPT_TIER_SNIPE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            calibration_min_samples=int(_envf("PULSE_CALIB_MIN_SAMPLES", 30)),
            calibration_max_shrink=_envf("PULSE_CALIB_MAX_SHRINK", 0.5),
            tv_context_gate_enabled=str(os.getenv("PULSE_TV_CONTEXT_GATE", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_context_blocked_volume_states=tuple(
                s.strip().lower() for s in os.getenv("PULSE_TV_CONTEXT_BLOCK_VOLUME", "spike")
                .split(",") if s.strip()),
            tv_context_blocked_hurst_regimes=tuple(
                s.strip().lower() for s in os.getenv("PULSE_TV_CONTEXT_BLOCK_HURST", "noise")
                .split(",") if s.strip()),
            tv_context_max_ttc_s=_envf("PULSE_TV_CONTEXT_MAX_TTC_S", 240.0),
            tv_context_block_liquidation_spike=str(
                os.getenv("PULSE_TV_CONTEXT_BLOCK_LIQUIDATION", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_context_block_event_blackout=str(
                os.getenv("PULSE_TV_CONTEXT_BLOCK_EVENT_BLACKOUT", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_context_block_grok_event_risk_high=str(
                os.getenv("PULSE_TV_CONTEXT_BLOCK_GROK_EVENT_RISK", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_context_exploration_rate=_envf("PULSE_TV_CONTEXT_EXPLORATION_RATE", 0.0),
            tv_down_bias_gate_enabled=str(os.getenv("PULSE_TV_DOWN_BIAS_GATE", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_down_bias_exploration_rate=_envf("PULSE_TV_DOWN_BIAS_EXPLORE_RATE", 0.0),
            tv_down_bias_block_up_on_bearish_down_stack=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_ON_BEARISH_DOWN_STACK", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_tv_down_non_bearish=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_TV_DOWN_NON_BEARISH", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_against_confirmed_down=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_AGAINST_CONFIRMED_DOWN", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_mixed_mtf_up=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_MIXED_MTF_UP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_bullish_supertrend_up=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_BULLISH_SUPERTREND_UP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_vwap_above=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_VWAP_ABOVE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_bb_expansion_up=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_BB_EXPANSION_UP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_range_breakout_down=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_RANGE_BREAKOUT_DOWN", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_bb_squeeze=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_BB_SQUEEZE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_range_top=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_RANGE_TOP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_markov_chop_noise=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_MARKOV_CHOP_NOISE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_htf_bullish=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_HTF_BULLISH", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_bear_close_near_low=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_BEAR_CLOSE_NEAR_LOW", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_medium_edge=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_MEDIUM_EDGE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_weak_cex=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_WEAK_CEX", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_late_ttc=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_LATE_TTC", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_early_ttc=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_EARLY_TTC", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_ask_heavy_ob=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_ASK_HEAVY_OB", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_tf_confirm_conflict=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_TF_CONFIRM_CONFLICT", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_cvd_neutral=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_CVD_NEUTRAL", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_cvd_buy_pressure=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_CVD_BUY_PRESSURE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_low_conviction=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_LOW_CONVICTION", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_bearish_mtf_tv_up=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_BEARISH_MTF_TV_UP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_mid_ttc=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_MID_TTC", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_neutral_zscore=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_NEUTRAL_ZSCORE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_medium_confidence=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_MEDIUM_CONFIDENCE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_not_stale=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_NOT_STALE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_volume_active=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_VOLUME_ACTIVE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_underdog_entry=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_UNDERDOG_ENTRY", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_up_underdog_entry_max=_envf(
                "PULSE_TV_DOWN_BIAS_UP_UNDERDOG_ENTRY_MAX", 0.55),
            tv_down_bias_up_late_ttc_min_s=_envf("PULSE_TV_DOWN_BIAS_UP_LATE_TTC_MIN_S", 240.0),
            tv_down_bias_up_early_ttc_max_s=_envf("PULSE_TV_DOWN_BIAS_UP_EARLY_TTC_MAX_S", 120.0),
            tv_down_bias_up_mid_ttc_min_s=_envf("PULSE_TV_DOWN_BIAS_UP_MID_TTC_MIN_S", 120.0),
            tv_down_bias_up_mid_ttc_max_s=_envf("PULSE_TV_DOWN_BIAS_UP_MID_TTC_MAX_S", 180.0),
            tv_down_bias_up_min_conviction=_envf("PULSE_TV_DOWN_BIAS_UP_MIN_CONVICTION", 0.40),
            tv_mtf_conflict_gate_enabled=str(os.getenv("PULSE_TV_MTF_CONFLICT_GATE", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_mtf_require_confirm=str(os.getenv("PULSE_TV_MTF_REQUIRE_CONFIRM", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_mtf_require_all_confirm=str(os.getenv("PULSE_TV_MTF_REQUIRE_ALL_CONFIRM", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_mtf_require_side_align=str(os.getenv("PULSE_TV_MTF_REQUIRE_SIDE_ALIGN", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_mtf_conflict_exploration_rate=_envf("PULSE_TV_MTF_CONFLICT_EXPLORE_RATE", 0.0),
            stop_enabled=str(os.getenv("PULSE_STOP_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            stop_rolling_n=int(_envf("PULSE_STOP_ROLLING_N", 50)),
            stop_min_samples=int(_envf("PULSE_STOP_MIN_SAMPLES", 30)),
            stop_min_profit_factor=_envf("PULSE_STOP_MIN_PROFIT_FACTOR", 0.85),
            stop_max_drawdown_pct=_envf("PULSE_STOP_MAX_DRAWDOWN_PCT", 25.0),
            late_window_entry_enabled=str(os.getenv("PULSE_LATE_WINDOW_ENTRY", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            late_window_max_ttc_s=_envf("PULSE_LATE_WINDOW_MAX_TTC_S", 120.0),
            late_window_min_conviction=_envf("PULSE_LATE_WINDOW_MIN_CONVICTION", 0.40),
            signal_engine_enabled=str(os.getenv("HERMES_SIGNAL_ENGINE_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            factor_model_enabled=str(os.getenv("HERMES_FACTOR_MODEL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            markov_enabled=str(os.getenv("HERMES_MARKOV_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_model_enabled=str(os.getenv("HERMES_EDGE_MODEL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            learning_enabled=str(os.getenv("PULSE_LEARNING_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            learning_min_samples=int(_envf("PULSE_LEARNING_MIN_SAMPLES", 60)),
            learning_bench_min_samples=int(_envf("PULSE_LEARNING_BENCH_MIN_SAMPLES", 20)),
            learning_bench_margin=_envf("PULSE_LEARNING_BENCH_MARGIN", 0.0),
            learning_max_weight=_envf("PULSE_LEARNING_MAX_WEIGHT", 0.5),
            learning_ramp_samples=_envf("PULSE_LEARNING_RAMP_SAMPLES", 300.0),
            learning_max_calib_error=_envf("PULSE_LEARNING_MAX_CALIB_ERROR", 0.15),
            sizing_enabled=str(os.getenv("HERMES_SIZING_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            sizing_hard_cap_usd=_envf("HERMES_SIZING_HARD_CAP_USD", 10.0),
            sizing_daily_loss_cap_usd=_envf("HERMES_SIZING_DAILY_LOSS_CAP_USD", 50.0),
            sizing_bankroll_usd=_envf("HERMES_SIZING_BANKROLL_USD", 1000.0),
            osmani_autonomous_sizing=str(os.getenv("PULSE_OSMANI_AUTONOMOUS_SIZING", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            osmani_sizing_min_usd=_envf("PULSE_OSMANI_SIZING_MIN_USD", 1.0),
            gate_auto_tune_enabled=str(os.getenv("PULSE_GATE_AUTO_TUNE_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            gate_auto_tune_lookback_n=int(_envf("PULSE_GATE_AUTO_TUNE_LOOKBACK_N", 24)),
            gate_auto_tune_min_samples=int(_envf("PULSE_GATE_AUTO_TUNE_MIN_SAMPLES", 12)),
            gate_auto_tune_target_wr=_envf("PULSE_GATE_AUTO_TUNE_TARGET_WR", 0.65),
            gate_auto_tune_kill_wr=_envf("PULSE_GATE_AUTO_TUNE_KILL_WR", 0.50),
            gate_auto_tune_starve_fph=_envf("PULSE_GATE_AUTO_TUNE_STARVE_FPH", 0.8),
            gate_auto_tune_rich_fph=_envf("PULSE_GATE_AUTO_TUNE_RICH_FPH", 3.0),
            gate_auto_tune_cooldown=int(_envf("PULSE_GATE_AUTO_TUNE_COOLDOWN", 6)),
            starting_capital_usd=_envf("PULSE_STARTING_CAPITAL_USD", 500.0),
            tradingview_secret=(os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "") or "").strip(),
            tradingview_allowed_symbols=tuple(
                s.strip().upper() for s in os.getenv(
                    "TRADINGVIEW_ALLOWED_SYMBOLS",
                    "BTCUSD,INDEX:BTCUSD,BTC/USD,BTC,XBTUSD").split(",")
                if s.strip()),
            # bot name: TRADINGVIEW_BOT_NAME takes precedence, else BOT_NAME, else "hermes"
            tradingview_bot_name=((os.getenv("TRADINGVIEW_BOT_NAME") or os.getenv("BOT_NAME")
                                   or "hermes").strip()),
            tradingview_event_id_suffix=(
                os.getenv("PULSE_TV_EVENT_ID_SUFFIX", "") or "").strip().lower(),
            tradingview_webhook_host=(os.getenv("TRADINGVIEW_WEBHOOK_HOST", "127.0.0.1")
                                      or "127.0.0.1").strip(),
            tradingview_webhook_port=int(_envf("TRADINGVIEW_WEBHOOK_PORT", 8787)),
            tradingview_webhook_path=(os.getenv("TRADINGVIEW_WEBHOOK_PATH", "/webhooks/tradingview")
                                      or "/webhooks/tradingview").strip(),
            tradingview_max_age_s=_envf("TRADINGVIEW_MAX_AGE_S", 90.0),
            tradingview_feature_symbol=normalize_symbol(
                os.getenv("PULSE_TV_FEATURE_SYMBOL", "BTCUSD") or "BTCUSD") or "BTCUSD",
            tradingview_mtf_timeframes=_parse_tv_mtf_timeframes(
                os.getenv("PULSE_TV_MTF_TIMEFRAMES", "5,15,30,45,60,240,1440")),
            tradingview_drop_timeframes=_parse_tv_drop_timeframes(
                os.getenv("PULSE_TV_DROP_TIMEFRAMES", "")),
            tradingview_allowed_bot_names=tuple(
                s.strip() for s in (os.getenv("PULSE_TV_ALLOWED_BOT_NAMES", "") or "").split(",")
                if s.strip()),
            tradingview_mtf_confirm_window_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_S", 360.0),
            tradingview_mtf_confirm_window_10m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_10M_S", 660.0),
            tradingview_mtf_confirm_window_15m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_15M_S", 2250.0),
            tradingview_mtf_confirm_window_2m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_2M_S", 300.0),
            tradingview_mtf_confirm_window_3m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_3M_S", 1200.0),
            tradingview_mtf_confirm_window_4m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_4M_S", 1500.0),
            tradingview_mtf_confirm_window_5m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_5M_S", 1500.0),
            tradingview_mtf_confirm_window_13m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_13M_S", 840.0),
            tradingview_mtf_confirm_window_30m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_30M_S", 4500.0),
            tradingview_mtf_confirm_window_45m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_45M_S", 6750.0),
            tradingview_mtf_confirm_window_55m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_55M_S", 8250.0),
            tradingview_alert_history_per_symbol=int(
                _envf("PULSE_TV_ALERT_HISTORY_PER_SYMBOL", 50)),
            tv_15m_short_path_n=int(_envf("PULSE_TV_15M_SHORT_PATH_N", 8)),
            tv_15m_chart_lean_enabled=str(
                os.getenv("PULSE_TV_15M_CHART_LEAN_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_15m_chart_lean_size=str(
                os.getenv("PULSE_TV_15M_CHART_LEAN_SIZE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_1h_short_path_n=int(_envf("PULSE_TV_1H_SHORT_PATH_N", 6)),
            tv_1h_chart_lean_enabled=str(
                os.getenv("PULSE_TV_1H_CHART_LEAN_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_1h_chart_lean_gate=str(
                os.getenv("PULSE_TV_1H_CHART_LEAN_GATE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_1h_chart_lean_size=str(
                os.getenv("PULSE_TV_1H_CHART_LEAN_SIZE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tradingview_rsi_div_history_per_symbol=int(
                _envf("PULSE_TV_RSI_DIV_HISTORY_PER_SYMBOL", 20)),
            tv_rsi_overlay_enabled=str(
                os.getenv("PULSE_TV_RSI_OVERLAY_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_rsi_overlay_size=str(
                os.getenv("PULSE_TV_RSI_OVERLAY_SIZE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_rsi_overlay_max_age_s=_envf("PULSE_TV_RSI_OVERLAY_MAX_AGE_S", 2700.0),
            tv_rsi_overlay_aligned_mult=_envf("PULSE_TV_RSI_OVERLAY_ALIGNED_MULT", 1.15),
            tv_rsi_overlay_opposed_mult=_envf("PULSE_TV_RSI_OVERLAY_OPPOSED_MULT", 0.45),
            binary_intel_enabled=str(
                os.getenv("PULSE_BINARY_INTEL_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            binary_intel_grok_compute=str(
                os.getenv("PULSE_BINARY_INTEL_GROK_COMPUTE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            binary_intel_min_score=_envf("PULSE_BINARY_INTEL_MIN_SCORE", 0.28),
            binary_intel_exploration_rate=_envf("PULSE_BINARY_INTEL_EXPLORATION_RATE", 0.05),
            binary_intel_min_size_scale=_envf("PULSE_BINARY_INTEL_MIN_SIZE_SCALE", 0.40),
            binary_intel_kelly_fraction=_envf("PULSE_BINARY_INTEL_KELLY_FRACTION", 0.25),
            sawr_enabled=str(os.getenv("PULSE_SAWR_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            sawr_lookback_n=int(_envf("PULSE_SAWR_LOOKBACK_N", 40)),
            sawr_min_samples=int(_envf("PULSE_SAWR_MIN_SAMPLES", 8)),
            sawr_target_wr=_envf("PULSE_SAWR_TARGET_WR", 0.60),
            sawr_kill_wr=_envf("PULSE_SAWR_KILL_WR", 0.48),
            sawr_starve_fph=_envf("PULSE_SAWR_STARVE_FPH", 0.6),
            sawr_rich_fph=_envf("PULSE_SAWR_RICH_FPH", 4.0),
            sawr_wr_weight=_envf("PULSE_SAWR_WR_WEIGHT", 1.0),
            sawr_fill_weight=_envf("PULSE_SAWR_FILL_WEIGHT", 0.35),
            sawr_kill_penalty=_envf("PULSE_SAWR_KILL_PENALTY", 2.0),
            sawr_cooldown=int(_envf("PULSE_SAWR_COOLDOWN", 5)),
            chronos_enabled=str(os.getenv("PULSE_CHRONOS_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            chronos_min_cohort_n=int(_envf("PULSE_CHRONOS_MIN_COHORT_N", 4)),
            chronos_proceed_cvs=_envf("PULSE_CHRONOS_PROCEED_CVS", 0.05),
            chronos_exploration_rate=_envf("PULSE_CHRONOS_EXPLORATION_RATE", 0.12),
            chronos_kill_wr=_envf("PULSE_CHRONOS_KILL_WR", 0.48),
            tradingview_rsi_band_history_per_symbol=int(
                _envf("PULSE_TV_RSI_BAND_HISTORY_PER_SYMBOL", 50)),
            tv_rsi_band_enabled=str(
                os.getenv("PULSE_TV_RSI_BAND_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_rsi_band_max_age_s=_envf("PULSE_TV_RSI_BAND_MAX_AGE_S", 900.0),
            tv_rsi_divergence_analysis_enabled=str(
                os.getenv("PULSE_TV_RSI_DIVERGENCE_ANALYSIS_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_2h_review_enabled=str(os.getenv("PULSE_TV_2H_REVIEW_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes"),
            tv_2h_lookback_s=_envf("PULSE_TV_2H_LOOKBACK_S", 7200.0),
            tv_2h_review_pretrade=str(os.getenv("PULSE_TV_2H_REVIEW_PRETRADE", "1"))
            .strip().lower() in ("1", "true", "yes"),
            tv_2h_council_grade=str(os.getenv("PULSE_TV_2H_COUNCIL_GRADE", "0"))
            .strip().lower() in ("1", "true", "yes"),
            tv_2h_alert_history_cap=int(_envf("PULSE_TV_2H_ALERT_HISTORY_CAP", 50)),
            pulse_series_slugs=_series_slugs,
            tradingview_signal_max_feature_age_s=_envf("PULSE_TV_SIGNAL_MAX_FEATURE_AGE_S", 3600.0),
            tradingview_signal_gate_enabled=str(os.getenv("PULSE_TRADINGVIEW_SIGNAL_GATE", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tradingview_min_signal_strength=_envf("PULSE_TV_MIN_SIGNAL_STRENGTH", 0.0),
            tv_confidence_tier_enabled=str(
                os.getenv("PULSE_TV_CONFIDENCE_TIER_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_tier_require_sweet_spot=str(
                os.getenv("PULSE_TV_TIER_REQUIRE_SWEET_SPOT", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_tier_15m_only=str(os.getenv("PULSE_TV_TIER_15M_ONLY", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_tier_aligned_strength_min=_envf("PULSE_TV_TIER_ALIGNED_STRENGTH_MIN", 0.72),
            tv_tier_a_min_edge_delta=_envf("PULSE_TV_TIER_A_MIN_EDGE_DELTA", -0.005),
            tv_tier_a_max_price_delta=_envf("PULSE_TV_TIER_A_MAX_PRICE_DELTA", 0.02),
            tv_tier_c_min_edge_delta=_envf("PULSE_TV_TIER_C_MIN_EDGE_DELTA", 0.005),
            tv_tier_c_max_price_delta=_envf("PULSE_TV_TIER_C_MAX_PRICE_DELTA", -0.03),
            tradingview_signal_horizon_s=_envf("PULSE_TV_SIGNAL_HORIZON_S", 300.0),
            tradingview_promotion_allowed=str(os.getenv("PULSE_TV_PROMOTION_ALLOWED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tradingview_promotion_min_samples=int(_envf("PULSE_TV_PROMOTION_MIN_SAMPLES", 50)),
            tradingview_promotion_min_win_rate=_envf("PULSE_TV_PROMOTION_MIN_WIN_RATE", 0.80),
            data_dir=os.getenv("HTE_DATA_DIR", "/data"))


class PulseEngine:
    def __init__(self, cfg: Optional[PulseConfig] = None, *, market_feed=None,
                 price_feed=None):
        self.cfg = cfg or PulseConfig()
        # reject classic Chainlink Data Feed / AggregatorV3 as the primary settlement feed
        from engine.pulse.oracle import validate_oracle_feed_type, LeadFeeds
        self.oracle_feed_type = validate_oracle_feed_type(self.cfg.oracle_feed_type)
        # Directional 1h + 15m feeds (BTC/ETH up/down).
        self._directional_hourly_feed = None
        self._directional_15m_feed = None
        if market_feed is None and (
                self.cfg.directional_event_slugs or self.cfg.directional_hourly_discover):
            try:
                from engine.pulse.directional_hourly_feed import DirectionalHourlyMarketFeed
                self._directional_hourly_feed = DirectionalHourlyMarketFeed(
                    explicit_slugs=self.cfg.directional_event_slugs,
                    auto_discover=bool(self.cfg.directional_hourly_discover))
            except Exception:  # noqa: BLE001
                self._directional_hourly_feed = None
        if market_feed is None and bool(getattr(self.cfg, "directional_15m_discover", True)):
            try:
                from engine.pulse.directional_15m_feed import Directional15mMarketFeed
                _dir_slugs = tuple(
                    str(s).lower() for s in (self.cfg.directional_series_slugs or ()))
                _assets = []
                for s in _dir_slugs:
                    if "15m" not in s:
                        continue
                    if "eth" in s and "eth" not in _assets:
                        _assets.append("eth")
                    elif "btc" in s and "btc" not in _assets:
                        _assets.append("btc")
                if not _assets:
                    _assets = ["btc", "eth"]
                self._directional_15m_feed = Directional15mMarketFeed(
                    auto_discover=True, assets=tuple(_assets))
            except Exception:  # noqa: BLE001
                self._directional_15m_feed = None
        from types import SimpleNamespace
        self._gamma_feed = SimpleNamespace(fetch_resolution=self._gamma_feed_resolve)
        self.rtds = None
        self._eth_rtds = None
        # ETH directional oracle: separate price feed so ETH up/down windows are priced on ETH's own
        # spot + volatility (never BTC's). Built when any ETH directional window can appear. PAPER ONLY.
        self._eth_price = None
        self._btc_hourly_price = None
        self._eth_hourly_price = None
        _need_eth = self._needs_eth_oracle()
        if price_feed is not None:
            self.price = price_feed
        elif self.cfg.rtds_enabled:
            # CANONICAL oracle: Chainlink ref price via Polymarket RTDS crypto_prices_chainlink.
            from engine.pulse.rtds import RTDSClient, TOPIC_CHAINLINK, TOPIC_BINANCE
            # RTDS currently honors only the first symbol for repeated topic subscriptions on one
            # connection. Keep BTC and ETH on independent sockets so neither asset silently starves.
            _subs = [(TOPIC_CHAINLINK, self.cfg.oracle_symbol), (TOPIC_BINANCE, "btcusdt")]
            self.rtds = RTDSClient(subscriptions=_subs)
            self.rtds.max_age_s = float(self.cfg.rtds_max_age_s)
            self.rtds.start()
            # poll the FRESH oracle price: a stale/dead socket returns None so the feed fails CLOSED
            # (last_ts stops advancing) instead of serving an aged cached level as 'live'.
            _rtds = self.rtds
            self.price = PulsePriceFeed(
                fetcher=lambda: _rtds.fresh_oracle_price(self.cfg.rtds_max_age_s),
                source_name="rtds_chainlink",
                vol=RollingVol(window_s=self.cfg.vol_window_s),
                max_open_lag_s=self.cfg.max_open_lag_s,
                max_open_lag_15m_s=self.cfg.max_open_lag_15m_s,
                sampler_interval_s=self.cfg.price_sampler_interval_s)
            self.price.start_sampler()
            # Hourly contracts settle from finalized Binance *USDT candles, not Chainlink.  Keep
            # independent continuously sampled feeds so the exact hourly boundary and volatility
            # are measured on the same source as the contract rules.
            self._btc_hourly_price = PulsePriceFeed(
                fetcher=lambda: _rtds.fresh_price(TOPIC_BINANCE, "btcusdt",
                                                   self.cfg.rtds_max_age_s),
                source_name="rtds_binance_btcusdt",
                vol=RollingVol(window_s=max(3600.0, self.cfg.vol_window_s)),
                max_open_lag_s=self.cfg.max_open_lag_s,
                max_open_lag_15m_s=self.cfg.max_open_lag_s,
                sampler_interval_s=self.cfg.price_sampler_interval_s)
            self._btc_hourly_price.start_sampler()
            if _need_eth:
                self._eth_rtds = RTDSClient(subscriptions=[
                    (TOPIC_CHAINLINK, "eth/usd"), (TOPIC_BINANCE, "ethusdt")])
                self._eth_rtds.max_age_s = float(self.cfg.rtds_max_age_s)
                self._eth_rtds.start()
                _eth_rtds = self._eth_rtds
                # Chainlink eth/usd via RTDS is the canonical ETH oracle; Coinbase ETH-USD is a
                # low-latency proxy fallback so ETH windows can still be priced if RTDS drops the
                # eth/usd symbol (same basis-cancels-in-close-open discipline as the BTC proxy path).
                from engine.pulse.coinbase import coinbase_spot_fetcher as _cb
                _eth_cb = _cb("ETH-USD")
                _eth_max_age = self.cfg.rtds_max_age_s

                def _eth_fetch(_rtds=_eth_rtds, _cb_fetch=_eth_cb, _max_age=_eth_max_age):
                    px = _rtds.fresh_price(TOPIC_CHAINLINK, "eth/usd", _max_age)
                    if px is not None and px > 0:
                        return px
                    return _cb_fetch()

                self._eth_price = PulsePriceFeed(
                    fetcher=_eth_fetch,
                    source_name="rtds_chainlink_eth",
                    vol=RollingVol(window_s=self.cfg.vol_window_s),
                    max_open_lag_s=self.cfg.max_open_lag_s,
                    max_open_lag_15m_s=self.cfg.max_open_lag_15m_s,
                    sampler_interval_s=self.cfg.price_sampler_interval_s)
                self._eth_price.start_sampler()
                self._eth_hourly_price = PulsePriceFeed(
                    fetcher=lambda: _eth_rtds.fresh_price(TOPIC_BINANCE, "ethusdt",
                                                           self.cfg.rtds_max_age_s),
                    source_name="rtds_binance_ethusdt",
                    vol=RollingVol(window_s=max(3600.0, self.cfg.vol_window_s)),
                    max_open_lag_s=self.cfg.max_open_lag_s,
                    max_open_lag_15m_s=self.cfg.max_open_lag_s,
                    sampler_interval_s=self.cfg.price_sampler_interval_s)
                self._eth_hourly_price.start_sampler()
        else:
            fetcher, src = build_price_source(self.cfg.price_source)
            self.price = PulsePriceFeed(
                fetcher=fetcher, source_name=src,
                vol=RollingVol(window_s=self.cfg.vol_window_s),
                max_open_lag_s=self.cfg.max_open_lag_s,
                max_open_lag_15m_s=self.cfg.max_open_lag_15m_s,
                sampler_interval_s=self.cfg.price_sampler_interval_s)
            self.price.start_sampler()
            if _need_eth:
                from engine.pulse.coinbase import coinbase_spot_fetcher
                self._eth_price = PulsePriceFeed(
                    fetcher=coinbase_spot_fetcher("ETH-USD"),
                    source_name="coinbase_eth",
                    vol=RollingVol(window_s=self.cfg.vol_window_s),
                    max_open_lag_s=self.cfg.max_open_lag_s,
                    max_open_lag_15m_s=self.cfg.max_open_lag_15m_s,
                    sampler_interval_s=self.cfg.price_sampler_interval_s)
                self._eth_price.start_sampler()
        # fast LEAD feeds (Binance via RTDS, Coinbase via REST) — FEATURES ONLY, never truth
        self.leads = LeadFeeds(self.cfg.fast_feeds, rtds=self.rtds,
                               window_s=self.cfg.vol_window_s)
        self.ledger = PulseLedger()
        self.calib = PulseCalibration()
        # OBSERVE-ONLY research features (EP Chan-inspired) — logged, never trade/size/veto.
        self.research = None
        if bool(getattr(self.cfg, "research_features_enabled", True)):
            from engine.pulse.research_features import ResearchObservatory
            self.research = ResearchObservatory()
        self.signals = None
        if bool(getattr(self.cfg, "signal_engine_enabled", True)):
            from engine.pulse.signals import SignalEngine
            self.signals = SignalEngine()
        self.factors = None
        if bool(getattr(self.cfg, "factor_model_enabled", True)):
            from engine.pulse.factors import FactorEngine
            self.factors = FactorEngine()
        self.markov = None
        if bool(getattr(self.cfg, "markov_enabled", True)):
            from engine.pulse.markov import MarkovRegime
            self.markov = MarkovRegime()
        self.edge_model = None
        if bool(getattr(self.cfg, "edge_model_enabled", True)):
            from engine.pulse.edge_model import EdgeModel
            self.edge_model = EdgeModel()
        # Learned Selectivity Gate v1 — live-evidence bucket gate between decision and execution.
        from engine.pulse.late_window import LateWindowEntry, LateWindowEdge
        self.late_window_gate = LateWindowEntry(
            enabled=bool(self.cfg.late_window_entry_enabled),
            max_ttc_s=self.cfg.late_window_max_ttc_s,
            min_conviction=self.cfg.late_window_min_conviction)
        self.late_window_edge = LateWindowEdge(   # OBSERVE-ONLY time-decay edge measurement
            max_ttc_s=self.cfg.late_window_max_ttc_s,
            min_conviction=self.cfg.late_window_min_conviction)
        from engine.pulse.config_coupling import (
            apply_context_cohort_coupling, window_seconds_for_slugs)
        _ctx_max, self._config_coupling = apply_context_cohort_coupling(
            baseline_cohort_enabled=bool(self.cfg.baseline_cohort_gate_enabled),
            tv_context_enabled=bool(self.cfg.tv_context_gate_enabled),
            configured_context_max_ttc_s=self.cfg.tv_context_max_ttc_s,
            cohort_ttc_min_s=self.cfg.baseline_cohort_ttc_min_s,
            cohort_ttc_max_s=self.cfg.baseline_cohort_ttc_max_s,
            window_seconds_list=window_seconds_for_slugs(self.cfg.pulse_series_slugs),
        )
        if self._config_coupling.get("auto_clamped"):
            logger.warning(
                "PULSE_TV_CONTEXT_MAX_TTC_S=%s below required %s for baseline cohort "
                "— auto-raised effective max to %s",
                self._config_coupling.get("configured_s"),
                self._config_coupling.get("required_min_s"),
                self._config_coupling.get("effective_s"),
            )
        elif self._config_coupling.get("active") and not self._config_coupling.get("configured_ok"):
            logger.error(
                "Gate coupling deadlock: PULSE_TV_CONTEXT_MAX_TTC_S=%s; need >= %s. %s",
                self._config_coupling.get("configured_s"),
                self._config_coupling.get("required_min_s"),
                self._config_coupling.get("fix_hint"),
            )
        from engine.pulse.context_gate import TradingViewContextGate
        self.tv_context_gate = TradingViewContextGate(
            enabled=bool(self.cfg.tv_context_gate_enabled),
            blocked_volume_states=self.cfg.tv_context_blocked_volume_states,
            blocked_hurst_regimes=self.cfg.tv_context_blocked_hurst_regimes,
            max_ttc_s=_ctx_max,
            block_liquidation_spike=self.cfg.tv_context_block_liquidation_spike,
            block_event_blackout=self.cfg.tv_context_block_event_blackout,
            block_grok_event_risk_high=self.cfg.tv_context_block_grok_event_risk_high,
            exploration_rate=self.cfg.tv_context_exploration_rate)
        from engine.pulse.tv_down_bias_gate import TradingViewDownBiasGate
        self.tv_down_bias_gate = TradingViewDownBiasGate(
            enabled=bool(self.cfg.tv_down_bias_gate_enabled),
            block_up_on_bearish_down_stack=bool(
                self.cfg.tv_down_bias_block_up_on_bearish_down_stack),
            block_up_tv_down_non_bearish=bool(
                self.cfg.tv_down_bias_block_up_tv_down_non_bearish),
            block_up_against_confirmed_down=bool(
                self.cfg.tv_down_bias_block_up_against_confirmed_down),
            block_mixed_mtf_up=bool(self.cfg.tv_down_bias_block_mixed_mtf_up),
            block_bullish_supertrend_up=bool(
                self.cfg.tv_down_bias_block_bullish_supertrend_up),
            block_up_vwap_above=bool(self.cfg.tv_down_bias_block_up_vwap_above),
            block_up_bb_expansion_up=bool(self.cfg.tv_down_bias_block_up_bb_expansion_up),
            block_up_range_breakout_down=bool(
                self.cfg.tv_down_bias_block_up_range_breakout_down),
            block_up_range_top=bool(self.cfg.tv_down_bias_block_up_range_top),
            block_up_bb_squeeze=bool(self.cfg.tv_down_bias_block_up_bb_squeeze),
            block_up_markov_chop_noise=bool(
                self.cfg.tv_down_bias_block_up_markov_chop_noise),
            block_up_htf_bullish=bool(self.cfg.tv_down_bias_block_up_htf_bullish),
            block_up_bear_close_near_low=bool(
                self.cfg.tv_down_bias_block_up_bear_close_near_low),
            block_up_medium_edge=bool(self.cfg.tv_down_bias_block_up_medium_edge),
            block_up_weak_cex=bool(self.cfg.tv_down_bias_block_up_weak_cex),
            block_up_late_ttc=bool(self.cfg.tv_down_bias_block_up_late_ttc),
            block_up_early_ttc=bool(self.cfg.tv_down_bias_block_up_early_ttc),
            block_up_ask_heavy_ob=bool(self.cfg.tv_down_bias_block_up_ask_heavy_ob),
            block_up_tf_confirm_conflict=bool(
                self.cfg.tv_down_bias_block_up_tf_confirm_conflict),
            block_up_cvd_neutral=bool(self.cfg.tv_down_bias_block_up_cvd_neutral),
            block_up_cvd_buy_pressure=bool(self.cfg.tv_down_bias_block_up_cvd_buy_pressure),
            block_up_low_conviction=bool(self.cfg.tv_down_bias_block_up_low_conviction),
            block_up_bearish_mtf_tv_up=bool(self.cfg.tv_down_bias_block_up_bearish_mtf_tv_up),
            block_up_mid_ttc=bool(self.cfg.tv_down_bias_block_up_mid_ttc),
            block_up_neutral_zscore=bool(self.cfg.tv_down_bias_block_up_neutral_zscore),
            block_up_medium_confidence=bool(self.cfg.tv_down_bias_block_up_medium_confidence),
            block_up_not_stale=bool(self.cfg.tv_down_bias_block_up_not_stale),
            block_up_volume_active=bool(self.cfg.tv_down_bias_block_up_volume_active),
            block_up_underdog_entry=bool(self.cfg.tv_down_bias_block_up_underdog_entry),
            up_underdog_entry_max=self.cfg.tv_down_bias_up_underdog_entry_max,
            up_late_ttc_min_s=self.cfg.tv_down_bias_up_late_ttc_min_s,
            up_early_ttc_max_s=self.cfg.tv_down_bias_up_early_ttc_max_s,
            up_mid_ttc_min_s=self.cfg.tv_down_bias_up_mid_ttc_min_s,
            up_mid_ttc_max_s=self.cfg.tv_down_bias_up_mid_ttc_max_s,
            up_min_conviction=self.cfg.tv_down_bias_up_min_conviction,
            exploration_rate=self.cfg.tv_down_bias_exploration_rate)
        from engine.pulse.tv_mtf_gate import TradingViewMtfConflictGate
        self.tv_mtf_gate = TradingViewMtfConflictGate(
            enabled=bool(self.cfg.tv_mtf_conflict_gate_enabled),
            require_confirm=bool(self.cfg.tv_mtf_require_confirm),
            require_all_confirm=bool(self.cfg.tv_mtf_require_all_confirm),
            require_side_align=bool(self.cfg.tv_mtf_require_side_align),
            exploration_rate=self.cfg.tv_mtf_conflict_exploration_rate)
        from engine.pulse.down_stack import DownStackGrader
        self.down_stack = DownStackGrader()
        from engine.pulse.stop_conditions import StrategyStopMonitor, StopConfig
        self.stop_monitor = StrategyStopMonitor(cfg=StopConfig(
            enabled=bool(self.cfg.stop_enabled),
            rolling_n=self.cfg.stop_rolling_n,
            min_samples=self.cfg.stop_min_samples,
            min_profit_factor=self.cfg.stop_min_profit_factor,
            max_drawdown_pct=self.cfg.stop_max_drawdown_pct,
            min_sharpe=float(self.cfg.stop_min_sharpe),
            sharpe_min_samples=int(self.cfg.stop_sharpe_min_samples)))
        from engine.pulse.clob_feed import ClobBookFeed
        self.clob_feed = ClobBookFeed(websocket_enabled=bool(self.cfg.clob_websocket_enabled))
        self._wire_clob_feed_metrics()
        from engine.pulse.selectivity import SelectivityEvidence, LearnedSelectivityGate
        self.selectivity_evidence = SelectivityEvidence()
        self.selectivity_gate = LearnedSelectivityGate(
            enabled=bool(self.cfg.selectivity_gate_enabled),
            min_samples=self.cfg.selectivity_min_samples,
            min_win_rate=self.cfg.selectivity_min_win_rate,
            min_profit_factor=self.cfg.selectivity_min_profit_factor,
            fdr_q=self.cfg.selectivity_fdr_q,
            confidence_z=self.cfg.selectivity_confidence_z,
            exploration_rate=self.cfg.selectivity_exploration_rate)
        from engine.pulse.hourly_entry_timing import HourlyEntryEvidence, LearnedHourlyEntryGate
        self.hourly_entry_evidence = HourlyEntryEvidence()
        self.hourly_entry_gate = LearnedHourlyEntryGate(
            enabled=bool(self.cfg.hourly_entry_gate_enabled),
            min_seconds_since_open=self.cfg.hourly_min_seconds_since_open,
            max_seconds_since_open=self.cfg.hourly_max_seconds_since_open,
            min_samples=self.cfg.hourly_entry_min_samples,
            min_profit_factor=self.cfg.hourly_entry_min_profit_factor,
            fdr_q=self.cfg.hourly_entry_fdr_q,
            confidence_z=self.cfg.hourly_entry_confidence_z,
            exploration_rate=self.cfg.hourly_entry_exploration_rate)
        from engine.pulse.p_exec import ContextSelfTune
        self.p_exec_tune = ContextSelfTune(
            min_promote_n=int(getattr(self.cfg, "p_exec_min_promote_n", 40) or 40),
            explore_rate=float(getattr(self.cfg, "p_exec_explore_rate", 0.05) or 0.05))
        self.mc_scenario = None  # started after grok_budget exists (see below)
        from engine.pulse.pre_trade_analysis import PreTradeEvidence, PreTradeGate
        self.pre_trade_evidence = PreTradeEvidence()
        self.pre_trade_gate = PreTradeGate(
            enabled=bool(self.cfg.pre_trade_analysis_enabled),
            min_score=self.cfg.pre_trade_min_score,
            exploration_rate=self.cfg.pre_trade_exploration_rate,
            min_size_scale=self.cfg.pre_trade_min_size_scale,
            min_samples=self.cfg.pre_trade_evidence_min_samples)
        from engine.pulse.gate_auto_tune import GateAutoTuneConfig, GateAutoTuner
        self.gate_auto_tuner = GateAutoTuner(GateAutoTuneConfig(
            enabled=bool(self.cfg.gate_auto_tune_enabled),
            lookback_n=int(self.cfg.gate_auto_tune_lookback_n),
            min_samples=int(self.cfg.gate_auto_tune_min_samples),
            target_wr=float(self.cfg.gate_auto_tune_target_wr),
            kill_wr=float(self.cfg.gate_auto_tune_kill_wr),
            starve_fills_per_hour=float(self.cfg.gate_auto_tune_starve_fph),
            rich_fills_per_hour=float(self.cfg.gate_auto_tune_rich_fph),
            cooldown_settlements=int(self.cfg.gate_auto_tune_cooldown),
        ))
        # 15m directional lane strategy learner (separate from hourly GateAutoTuner).
        from engine.pulse.lane_15m_learner import (
            Lane15mLearnerConfig, Lane15mPolicy, Lane15mStrategyLearner)
        self.lane_15m_learner = Lane15mStrategyLearner(
            Lane15mLearnerConfig(
                enabled=bool(getattr(self.cfg, "lane_15m_learn_enabled", True)),
                target_wr=float(getattr(self.cfg, "lane_15m_target_wr", 0.60)),
                kill_wr=float(getattr(self.cfg, "lane_15m_kill_wr", 0.45)),
                min_samples=int(getattr(self.cfg, "lane_15m_min_samples", 10)),
            ),
            policy=Lane15mPolicy(),
        )
        # Shared 15m↔1h cross-horizon learner (restrict/size only; no new Loop Engineering lane).
        from engine.pulse.cross_horizon_learner import (
            CrossHorizonConfig, CrossHorizonLearner, CrossHorizonPolicy)
        self.cross_horizon_learner = CrossHorizonLearner(
            CrossHorizonConfig(
                enabled=bool(getattr(self.cfg, "cross_horizon_learn_enabled", True)),
                min_samples=int(getattr(self.cfg, "cross_horizon_min_samples", 20)),
                target_wr=float(getattr(self.cfg, "cross_horizon_target_wr", 0.60)),
                kill_wr=float(getattr(self.cfg, "cross_horizon_kill_wr", 0.45)),
                exploration_rate=float(getattr(self.cfg, "cross_horizon_exploration_rate", 0.08)),
            ),
            policy=CrossHorizonPolicy(),
        )
        # Binary Intel — invented pre/post-trade math + universal 5m TV + Grok protocols.
        from engine.pulse.binary_intel import BinaryIntelController
        self.binary_intel = BinaryIntelController(
            enabled=bool(getattr(self.cfg, "binary_intel_enabled", True)),
            grok_compute_enabled=bool(getattr(self.cfg, "binary_intel_grok_compute", True)),
            max_age_s=float(getattr(self.cfg, "tv_rsi_overlay_max_age_s", 2700.0) or 2700.0),
            kelly_fraction=float(getattr(self.cfg, "binary_intel_kelly_fraction", 0.25) or 0.25),
            aligned_mult=float(getattr(self.cfg, "tv_rsi_overlay_aligned_mult", 1.15) or 1.15),
            opposed_mult=float(getattr(self.cfg, "tv_rsi_overlay_opposed_mult", 0.45) or 0.45),
            min_intel_score=float(getattr(self.cfg, "binary_intel_min_score", 0.28) or 0.28),
            exploration_rate=float(getattr(self.cfg, "binary_intel_exploration_rate", 0.05) or 0.05),
            min_size_scale=float(getattr(self.cfg, "binary_intel_min_size_scale", 0.40) or 0.40),
        )
        # SAWR — invented Self-Adjusting Win-Rate meta-controller (Pareto + Beta affinity).
        from engine.pulse.sawr_controller import SawrConfig, SawrController
        self.sawr = SawrController(SawrConfig(
            enabled=bool(getattr(self.cfg, "sawr_enabled", True)),
            lookback_n=int(getattr(self.cfg, "sawr_lookback_n", 40)),
            min_samples=int(getattr(self.cfg, "sawr_min_samples", 8)),
            target_wr=float(getattr(self.cfg, "sawr_target_wr", 0.60)),
            kill_wr=float(getattr(self.cfg, "sawr_kill_wr", 0.48)),
            starve_fph=float(getattr(self.cfg, "sawr_starve_fph", 0.6)),
            rich_fph=float(getattr(self.cfg, "sawr_rich_fph", 4.0)),
            wr_weight=float(getattr(self.cfg, "sawr_wr_weight", 1.0)),
            fill_weight=float(getattr(self.cfg, "sawr_fill_weight", 0.35)),
            kill_penalty=float(getattr(self.cfg, "sawr_kill_penalty", 2.0)),
            cooldown_settlements=int(getattr(self.cfg, "sawr_cooldown", 5)),
        ))
        # CHRONOS — invented pre-decision walk-forward validator (dry-run before size/trade).
        from engine.pulse.chronos_validator import ChronosConfig, ChronosValidator
        self.chronos = ChronosValidator(ChronosConfig(
            enabled=bool(getattr(self.cfg, "chronos_enabled", True)),
            min_cohort_n=int(getattr(self.cfg, "chronos_min_cohort_n", 4)),
            proceed_cvs=float(getattr(self.cfg, "chronos_proceed_cvs", 0.05)),
            exploration_rate=float(getattr(self.cfg, "chronos_exploration_rate", 0.12)),
            kill_wr=float(getattr(self.cfg, "chronos_kill_wr", 0.48)),
        ))
        self.reconciler = LifecycleReconciler()   # GS-Quant-style candidate lifecycle audit
        self.gate_obs = GateObservations()        # orderbook-reality observations seen at the gate
        self._baseline: Optional[dict] = None     # legacy ledger totals that predate accounting
        self._report_epoch: dict = {}             # report anchor: trading metrics since this ts
        from engine.pulse.promotion import PromotionLadder
        self.promotion = PromotionLadder()        # all features default to observe-only (level 0)
        self._daily_loss = 0.0                    # for the Kelly daily-loss-cap diagnostic
        self._daily_key = None
        from engine.pulse.reporting import OutcomeGroups
        self._groups = OutcomeGroups()            # settled PnL grouped by every entry-time tag
        from engine.pulse.tradingview import (TradingViewEdge, RSITrendModel,
                                              TradingViewSignalLearner)
        self._tv_edge = TradingViewEdge()         # OBSERVE-ONLY TradingView signal-vs-outcome edge
        self._rsi_model = RSITrendModel()         # OBSERVE-ONLY RSI alert-history next-trend model
        self._tv_learner = TradingViewSignalLearner()   # OBSERVE-ONLY bucketed perf + promotion
        self._tv_pending: list = []               # pending forward-return evals for ALL signals
        # OBSERVE-ONLY BTC Pulse Edge Signal layer (CEX basket + stale divergence + OB pressure).
        self.edge_signal = None
        self._cex_extra: dict = {}                # optional Kraken/Bitstamp fetchers (opt-in)
        if bool(getattr(self.cfg, "edge_signal_enabled", True)):
            from engine.pulse.edge_signal import EdgeSignalEngine
            members = ["binance_btcusdt", "coinbase_btcusd"]
            if bool(self.cfg.edge_extra_cex_enabled):
                members += ["kraken_btcusd", "bitstamp_btcusd"]
                try:
                    from engine.pulse.cex_feeds import kraken_spot_fetcher, bitstamp_spot_fetcher
                    self._cex_extra = {"kraken_btcusd": kraken_spot_fetcher(),
                                       "bitstamp_btcusd": bitstamp_spot_fetcher()}
                except Exception:  # noqa: BLE001
                    self._cex_extra = {}
            self.edge_signal = EdgeSignalEngine(members)
        # CEX-lead latency edge (grades CEX-implied P(up) vs the market; PAPER ONLY, shadow default).
        self.cex_lead = None
        self._cex_lead_pending: list = []
        # directional allowlist cold-start exploration (avoids the proven-winning deadlock/freeze)
        self._allowlist_rng = random.Random(1729)
        self._allowlist_explored = 0
        self._allowlist_blocked = 0
        self._loop_synthesis_cache: dict = {}
        # market-beating benchmark for the learning blend: grade the edge model's P(up) vs the MARKET
        # price (poly_yes) per window; the blend only activates when the model actually beats the
        # market out-of-sample (kills phantom edge — calibrated != more accurate than the market).
        self._mkt_bench_pending: list = []
        self._mkt_bench_recent: deque = deque(maxlen=400)   # (model_se, market_se, fair_se)
        if bool(getattr(self.cfg, "cex_lead_enabled", True)):
            from engine.pulse.cex_lead import CexLeadEdge
            self.cex_lead = CexLeadEdge(
                enabled=True, mode=self.cfg.cex_lead_mode,
                min_samples=self.cfg.cex_lead_min_samples,
                min_divergence=self.cfg.cex_lead_min_divergence,
                confidence_z=self.cfg.cex_lead_confidence_z,
                min_edge_vs_market=self.cfg.cex_lead_min_edge_vs_market,
                tv_strength_thr=self.cfg.cex_lead_tv_strength_thr,
                decisive_thr=self.cfg.cex_lead_decisive_thr,
                late_ttc_s=self.cfg.cex_lead_late_ttc_s,
                kelly_scale=self.cfg.cex_lead_kelly_scale,
                max_size_frac=self.cfg.cex_lead_max_size_frac)
        self._ev_before_sum = 0.0                 # EV before/after costs (accepted candidates)
        self._ev_after_sum = 0.0
        self._ev_n = 0
        self._exec_realistic_samples: list = []
        self._payoff_guard_counts: dict = {
            "rejected_tiny_upside": 0,
            "rejected_bad_reward_to_risk": 0,
            "rejected_high_entry_insufficient_margin": 0,
        }
        self._last_simplex: dict = {}
        # ---- Grok consumers share ONE budget guard (daily $ cap + per-feature hourly calls) ----
        # All OBSERVE-ONLY / off hot path / fail-open; none can place, size, or bypass a trade.
        self.grok_budget = None
        self.overlay = None
        self.grok_analyst = None
        self.grok_predictor = None
        self.grok_decider = None
        self.grok_news = None
        self._grok_pending: list = []             # pending decision grades (decision_id/price0/close)
        self._grok_tv_fp: dict = {}               # decision_id -> last MTF fingerprint (refresh Grok)
        self._grok_entry_band_seen: set = set()   # windows that got entry-band Grok refresh
        self._verifier_pending: list = []        # pending verifier counterfactual grades at window close
        self._recent_windows: list = []           # rolling recent BTC 5m window outcomes (for Grok)
        import random as _random
        self._grok_rng = _random.Random()         # exploration sampler (follow-mode data gathering)
        self._grok_policy_counts = {"exploit": 0, "explore": 0, "avoid": 0}   # adaptive-loop tally
        self._mispricing_gate_counts: dict = {}
        self._tv_strong_fade_counts: dict = {}
        self._baseline_cohort_gate_counts: dict = {}
        self._tv_tier_counts: dict = {}
        try:
            from engine.pulse.grok_intel import (GrokBudget, GrokSignalAnalyst,
                                                 GrokSignalPredictor, xai_key)
            decider_on = str(self.cfg.grok_decider_mode).strip().lower() == "shadow"
            any_grok = (bool(self.cfg.grok_overlay_enabled)
                        or bool(self.cfg.grok_signal_analyst_enabled)
                        or bool(self.cfg.grok_signal_predictor_enabled)
                        or decider_on)
            if any_grok and xai_key():
                self.grok_budget = GrokBudget(
                    daily_usd_cap=self.cfg.grok_budget_daily_usd,
                    est_usd_per_call=self.cfg.grok_est_usd_per_call,
                    per_feature_hourly={"predictor": self.cfg.grok_predictor_max_calls_per_hour,
                                        "analyst": self.cfg.grok_analyst_max_calls_per_hour,
                                        "overlay": self.cfg.grok_overlay_max_calls_per_hour,
                                        "decider": self.cfg.grok_decider_max_calls_per_hour,
                                        "news": 40})
            if bool(self.cfg.grok_overlay_enabled) and xai_key():
                from engine.pulse.overlay import GrokEventOverlay
                self.overlay = GrokEventOverlay(
                    interval_s=self.cfg.grok_overlay_interval_s,
                    max_calls_per_hour=self.cfg.grok_overlay_max_calls_per_hour,
                    budget=self.grok_budget)
                self.overlay.start()
            if bool(self.cfg.grok_signal_predictor_enabled) and xai_key():
                self.grok_predictor = GrokSignalPredictor(budget=self.grok_budget).start()
            if bool(self.cfg.grok_signal_analyst_enabled) and xai_key():
                self.grok_analyst = GrokSignalAnalyst(
                    budget=self.grok_budget, interval_s=self.cfg.grok_analyst_interval_s,
                    report_provider=self._grok_analyst_report).start()
            if decider_on and xai_key():
                from engine.pulse.grok_decider import (GrokDecider, make_decider_fn,
                                                       GrokNewsDigest, make_news_fn)
                # news digest is a SEPARATE periodic search worker; the per-window decision reuses it
                # (cheaper/faster than searching every window). Enabled via use_search.
                if (bool(self.cfg.grok_news_enabled)
                        and (bool(self.cfg.grok_decider_use_search)
                             or self.cfg.grok_tiered_compute_enabled)):
                    self.grok_news = GrokNewsDigest(
                        budget=self.grok_budget,
                        news_fn=make_news_fn(model=self.cfg.grok_decider_model,
                                             timeout_s=max(35.0, self.cfg.grok_decider_timeout_s)),
                        interval_s=self.cfg.grok_news_refresh_s).start()
                self.grok_decider = GrokDecider(
                    decider_fn=make_decider_fn(
                        model=self.cfg.grok_decider_model,
                        timeout_s=self.cfg.grok_decider_timeout_s,
                        use_search=bool(self.cfg.grok_decider_use_search),
                        use_search_deep_only=True,
                        default_ttl_s=self.cfg.grok_decider_ttl_s),
                    budget=self.grok_budget, mode=self.cfg.grok_decider_mode,
                    min_confidence=self.cfg.grok_decider_min_confidence,
                    ttl_s=self.cfg.grok_decider_ttl_s).start()
        except Exception:  # noqa: BLE001 — Grok never blocks startup
            logger.exception("grok init failed; continuing as pure quant")
            self.grok_budget = self.overlay = self.grok_analyst = self.grok_predictor = None
            self.grok_decider = self.grok_news = None
        # Directional MC scenario advisor (Grok parameterizes; code simulates)
        if (self.mc_scenario is None
                and bool(getattr(self.cfg, "dir_mc_enabled", True))
                and bool(getattr(self.cfg, "mc_scenario_llm", True))):
            try:
                from engine.pulse.monte_carlo import (
                    MCScenarioAdvisor, make_grok_scenario_fn, make_claude_scenario_fn,
                    make_ensemble_scenario_fn)
                fns = [make_grok_scenario_fn(
                    model=getattr(self.cfg, "grok_decider_model", "grok-4.3"))]
                if bool(getattr(self.cfg, "mc_scenario_claude", False)):
                    fns.append(make_claude_scenario_fn())
                scen_fn = make_ensemble_scenario_fn(fns) if len(fns) > 1 else fns[0]
                self.mc_scenario = MCScenarioAdvisor(
                    scenario_fn=scen_fn,
                    budget=getattr(self, "grok_budget", None),
                    context_fn=self._mc_scenario_context,
                    interval_s=180.0, max_age_s=600.0,
                    feature="mc_scenario").start()
            except Exception:  # noqa: BLE001
                logger.exception("MC scenario advisor init failed; neutral params")
                self.mc_scenario = None
        # ---- #2 compounding lessons + #3 loop registry ----
        from engine.pulse.lessons import LessonsBook
        from engine.pulse.loops import LoopRegistry
        from engine.pulse.decision_history import TradeDecisionHistory
        self.lessons = LessonsBook(revalidate_ttl_s=self.cfg.lessons_revalidate_ttl_s)
        self.trade_history = TradeDecisionHistory(max_trades=50)
        self.loops = LoopRegistry()
        # ---- #1 independent Claude maker-checker verifier + #4 research meta-loop ----
        self.claude_budget = None
        self.verifier = None
        self.claude_decider = None             # Claude directional second-opinion (LLM council member)
        self.research_loop = None
        self._research_avoid: set = set()      # canonical "dim=bucket" contexts auto-blocked by Claude
        self._research_exploit: set = set()    # "dim=bucket" contexts Claude flags AND data proves WINNING
        try:
            from engine.pulse.claude_client import anthropic_key
            need_claude = (bool(self.cfg.verifier_enabled)
                           or bool(self.cfg.research_loop_enabled))
            if need_claude and anthropic_key():
                from engine.pulse.grok_intel import GrokBudget
                hourly = {
                    "verifier": self.cfg.verifier_max_calls_per_hour,
                    "research": self.cfg.research_max_calls_per_hour,
                }
                self.claude_budget = GrokBudget(
                    daily_usd_cap=self.cfg.claude_budget_daily_usd,
                    est_usd_per_call=self.cfg.claude_est_usd_per_call,
                    per_feature_hourly=hourly)
                if self.cfg.verifier_enabled:
                    from engine.pulse.verifier import ClaudeVerifier
                    self.verifier = ClaudeVerifier(
                        budget=self.claude_budget, enabled=True,
                        fail_open=self.cfg.verifier_fail_open,
                        explore_approve=self.cfg.verifier_explore_approve,
                        explore_max_size_fraction=float(
                            self.cfg.verifier_explore_max_size_fraction),
                    ).start()
                if self.cfg.research_loop_enabled:
                    from engine.pulse.research_loop import ResearchLoop
                    self.research_loop = ResearchLoop(
                        budget=self.claude_budget, interval_s=self.cfg.research_interval_s,
                        event_min_gap_s=self.cfg.research_event_min_gap_s,
                        report_provider=self._research_report, lessons=self.lessons,
                        apply_fn=self._research_apply,
                        auto_apply=self.cfg.research_auto_apply).start()
                if self.cfg.claude_decider_enabled:
                    from engine.pulse.claude_decider import (ClaudeDecider,
                                                             make_claude_decider_fn)
                    self.claude_decider = ClaudeDecider(
                        decider_fn=make_claude_decider_fn(
                            model=(self.cfg.claude_decider_model or None),
                            timeout_s=self.cfg.claude_decider_timeout_s),
                        budget=self.claude_budget,
                        ttl_s=self.cfg.grok_decider_ttl_s).start()
        except Exception:  # noqa: BLE001 — verifier/research never block startup
            logger.exception("claude verifier/research init failed; continuing")
            self.claude_budget = self.verifier = self.research_loop = None
            self.claude_decider = None
        # ---- LLM COUNCIL: evidence-weighted ensemble of quant + Grok + Claude views (PAPER) ----
        from engine.pulse.llm_council import LLMCouncil
        self.llm_council = LLMCouncil(
            enabled=bool(self.cfg.llm_council_enabled),
            min_agreement=self.cfg.llm_council_min_agreement,
            min_margin=self.cfg.llm_council_min_margin,
            min_members=self.cfg.llm_council_min_members)
        # Retire per-TF council members for dropped TradingView timeframes so their stale graded
        # stats vanish from the council + report (and old pending snapshots can't repopulate them).
        if self.cfg.tradingview_drop_timeframes:
            self.llm_council.forget("tv_%sm" % t for t in self.cfg.tradingview_drop_timeframes)
        self._council_pending: list = []
        # OBSERVE-ONLY TradingView indicator webhook intake (enabled only when a secret is set).
        # Alerts become candidate signals only; they can never place/resize/bypass a paper trade.
        self.tradingview = None
        self.webhook = None
        if str(getattr(self.cfg, "tradingview_secret", "") or "").strip():
            try:
                from engine.pulse.tradingview import TradingViewIntake
                from engine.pulse.webhook import WebhookServer
                self.tradingview = TradingViewIntake(
                    secret=self.cfg.tradingview_secret,
                    allowed_symbols=self.cfg.tradingview_allowed_symbols,
                    bot_name=self.cfg.tradingview_bot_name,
                    expected_event_id_suffix=self.cfg.tradingview_event_id_suffix,
                    max_age_s=self.cfg.tradingview_max_age_s, data_dir=self.cfg.data_dir,
                    feature_symbol=self.cfg.tradingview_feature_symbol,
                    mtf_timeframes=self.cfg.tradingview_mtf_timeframes,
                    confirm_windows_by_tf=_tv_mtf_confirm_windows(self.cfg),
                    confirm_window_s=self.cfg.tradingview_mtf_confirm_window_s,
                    confirm_window_10m_s=self.cfg.tradingview_mtf_confirm_window_10m_s,
                    confirm_window_15m_s=self.cfg.tradingview_mtf_confirm_window_15m_s,
                    drop_timeframes=self.cfg.tradingview_drop_timeframes,
                    allowed_bot_names=(self.cfg.tradingview_allowed_bot_names or None),
                    # Hard FIFO: last 50 alerts/symbol (operator mandate). Cap is the
                    # configured history size — do not inflate above it via 2h lookback.
                    alert_history_per_symbol=max(
                        1, int(self.cfg.tradingview_alert_history_per_symbol or 50)),
                    rsi_div_history_per_symbol=max(
                        1, int(getattr(self.cfg, "tradingview_rsi_div_history_per_symbol", 20) or 20)),
                    rsi_band_history_per_symbol=max(
                        1, int(getattr(self.cfg, "tradingview_rsi_band_history_per_symbol", 50) or 50)))
                self.webhook = WebhookServer(
                    self.tradingview, host=self.cfg.tradingview_webhook_host,
                    port=self.cfg.tradingview_webhook_port,
                    path=self.cfg.tradingview_webhook_path).start()
            except Exception:  # noqa: BLE001 — intake never blocks the paper loop
                logger.exception("tradingview webhook init failed; continuing without it")
                self.tradingview = None
                self.webhook = None
        self.osmani_loop = None
        if bool(getattr(self.cfg, "osmani_loop_enabled", False)):
            from engine.pulse.loop_architecture import OsmaniLoopCoordinator
            from engine.pulse.training_throughput import training_throughput_enabled, training_min_ev
            _exec_ev = float(self.cfg.exec_min_ev_after_slippage)
            if training_throughput_enabled():
                _exec_ev = training_min_ev()
            self.osmani_loop = OsmaniLoopCoordinator(
                data_dir=Path(self.cfg.data_dir),
                windows_fn=lambda now: self._osmani_directional_windows(now),
                fair_fn=self._osmani_fair_p,
                hydrate_snapshot_fn=self._osmani_hydrate_snapshot,
                execute_verified_fn=self._osmani_execute_verified,
                persist_fn=self._persist,
                capital_fn=self._capital_status,
                size_usd=float(self.cfg.size_usd),
                min_edge=float(self.cfg.min_edge),
                discovery_interval_s=float(self.cfg.osmani_discovery_interval_s),
                exec_min_ev=_exec_ev,
                exec_max_spread=float(self.cfg.exec_max_spread),
                min_entry_price=float(self.cfg.min_entry_price),
                beat_fn=self.loops.beat,
                tv_feature_fn=self._osmani_trend_feature,
                hourly_entry_fn=self._osmani_hourly_entry_check,
                triage_skill_enabled=bool(self.cfg.osmani_triage_skill_enabled),
                enabled=True,
            )
        self._register_loops()
        self.ticks = 0
        self.last_tick_ts = 0.0
        self._reasons: dict = {}
        self._last_eval: list = []
        # PRISM Phase 2 — observe-only information completeness tracker. Refreshed each tick from
        # the latest TV ladder + anchor; published under status["prism_information"]. Never gates a
        # trade (PRISM is wired into the decision path only in the final integration phase).
        from engine.pulse.prism.information import InformationTracker
        self.prism_info = InformationTracker()
        self._prism_info_report: dict = {"enabled": True, "I": 0.0, "note": "warming up"}
        # PRISM Phase 3 — optimal stopping (ENTER/WAIT/SKIP). Restrict-only; wired into the LEGACY
        # directional path only (never overrides Osmani authority). Default OFF via config flag.
        from engine.pulse.prism.stopping import PRISMConfig, StoppingEngine
        self.prism_stopping = StoppingEngine(PRISMConfig.from_env())
        # PRISM Phase 4 — observe-only ensemble edge snapshot (last directional candidate).
        self._prism_ensemble_report: dict = {"enabled": False}
        self._data_dir = Path(self.cfg.data_dir)
        # PRISM Phase 5 — Thompson bucket posteriors. Learns on every settle (observe-only); the
        # block-gate is restrict-only (legacy path) and OFF by default. BNB hard block configurable.
        from engine.pulse.prism.thompson import ThompsonStore
        self.prism_thompson = ThompsonStore(data_dir=self._data_dir,
                                            bnb_block=bool(self.cfg.prism_bnb_block))
        if getattr(self.cfg, "fresh_start", False):
            self.prism_thompson.buckets.clear()
        # PRISM Phase 6 — Sniper/Harvester agents + capital allocation + cross-asset lead-lag.
        # Observe-only: computed per directional candidate and published; the agent gate + size
        # override are restrict-only behind PULSE_PRISM_AGENT_GATE_ENABLED (default OFF).
        from engine.pulse.prism.agents import AgentConfig, CapitalAllocator
        self._prism_agent_cfg = AgentConfig.from_env()
        _bankroll = float(self.cfg.starting_capital_usd) * float(self.cfg.directional_max_bankroll_frac)
        self.prism_agents = CapitalAllocator(_bankroll, self._prism_agent_cfg)
        self._prism_leader_p: dict = {}
        self._prism_agent_report: dict = {"enabled": False}
        # Directional Tier Engine — regime-aware directional brain (drives paper directional entries).
        self.tier_engine = None
        if bool(self.cfg.tier_engine_enabled):
            from engine.pulse.tier_engine import DirectionalTierEngine, TierConfig
            self.tier_engine = DirectionalTierEngine(TierConfig.from_env(), data_dir=self._data_dir)
            # Report the authority actually wired into runtime; previously status claimed the tier
            # classifier was observe-only while it selected every traded side.
            self.promotion.levels["tier_classifier"] = 4
            self.promotion.levels["kelly_sizing"] = 3
            self.promotion.history.append({
                "feature": "tier_classifier", "to": 4,
                "reason": "configured_runtime_authority; still constrained by promotion gates"})
        self.cell_learning = None
        if bool(self.cfg.cell_learning_enabled):
            from engine.pulse.directional_cell_learning import DirectionalCellLearningStore
            self.cell_learning = DirectionalCellLearningStore(
                data_dir=self._data_dir, min_samples=self.cfg.cell_learning_min_samples)
        self._ledger_path = self._data_dir / "btc_pulse_ledger.json"
        from engine.pulse.performance_scoring import PerformanceScoreHistory
        self._score_history = PerformanceScoreHistory(
            self._data_dir / "btc_pulse_score_history.json")
        if not self.cfg.fresh_start:
            self._load_state()
        elif self._ledger_path.exists():
            self._archive_prior_state()
        self._maybe_reset_capital()   # token-gated SURGICAL capital reset (keeps all learning)
        self._ensure_report_epoch()
        self._resolve_baseline()

    @staticmethod
    def _selectivity_tags_from_pos(pos) -> dict:
        """Entry-time bucket tags for a settled position (settlement + counterfactual)."""
        rt = pos.research or {}
        return {"hurst_regime": rt.get("hurst_regime"), "zscore_bucket": rt.get("zscore_bucket"),
                "ttc_bucket": rt.get("ttc_bucket"),
                "hourly_entry_bucket": rt.get("hourly_entry_bucket"),
                "confidence_tier": rt.get("confidence_tier"),
                "spread_bucket": rt.get("spread_bucket"), "depth_bucket": rt.get("depth_bucket"),
                "markov_state": rt.get("markov_state"),
                "edge_quality_bucket": rt.get("edge_quality_bucket"),
                "stale_divergence": rt.get("edge_stale_divergence"), "direction": pos.side}

    def _selectivity_positions(self) -> list:
        """Settled positions as (tags, won, pnl) rows for the counterfactual replay."""
        rows = []
        for pos in self.ledger.positions.values():
            if pos.status == "settled":
                rows.append({"tags": self._selectivity_tags_from_pos(pos),
                             "won": bool(pos.won), "pnl": float(pos.pnl_usd or 0.0)})
        return rows

    def _maybe_reset_capital(self) -> None:
        """Token-gated SURGICAL reset: zero the paper CAPITAL / ledger / reconciliation
        back to a fresh ``starting_capital_usd`` while KEEPING everything the bot has LEARNED
        (probability models, calibration, selectivity evidence, lessons, signal gradings, research
        rules, CEX-lead/TV/Grok learning). Runs exactly ONCE per new ``PULSE_RESET_CAPITAL_TOKEN``
        (idempotent across restarts via a marker file). PAPER ONLY."""
        token = (os.getenv("PULSE_RESET_CAPITAL_TOKEN") or "").strip()
        if not token:
            return
        marker = self._data_dir / ".capital_reset_token"
        try:
            prior = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        except Exception:  # noqa: BLE001
            prior = ""
        if prior == token:
            return                      # this reset token was already applied — do nothing
        # --- reset ONLY money/operational state to fresh instances ---
        self.ledger = PulseLedger()
        self.gate_obs = GateObservations()
        self.reconciler = LifecycleReconciler()
        self._ev_before_sum = 0.0
        self._ev_after_sum = 0.0
        self._ev_n = 0
        self._allowlist_explored = 0
        self._allowlist_blocked = 0
        self._baseline = empty_baseline()
        self._reasons = {}
        self._last_eval = []
        # Trading context buffers (not graded learning — safe to wipe).
        from engine.pulse.decision_history import TradeDecisionHistory
        self.trade_history = TradeDecisionHistory(max_trades=50)
        self._recent_windows = []
        self._council_pending = []
        self._grok_pending = []
        self._verifier_pending = []
        self._tv_pending = []
        self._cex_lead_pending = []
        self._mkt_bench_pending = []
        self._mkt_bench_recent = deque(maxlen=400)
        if getattr(self, "_score_history", None) is not None:
            self._score_history._data = {"schema": "btc_pulse_score_history/1.0", "entries": []}
            self._score_history.save()
        from engine.pulse.report_epoch import make_epoch, write_epoch_file
        self._report_epoch = make_epoch(
            token=token,
            starting_capital_usd=float(self.cfg.starting_capital_usd),
            note="Capital reset — reports count trading from this point only.",
        )
        write_epoch_file(self._data_dir, self._report_epoch)
        # KEPT (learning, untouched): self.calib, self.edge_model, self.selectivity_evidence,
        #   self.selectivity_gate, self.lessons, self._research_avoid/_exploit, self.cex_lead,
        #   self._tv_edge/_rsi_model/_tv_learner, self.edge_signal, self.grok_*, self.verifier,
        #   self.llm_council, tv_context_gate, late_window_* .
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(token, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        logger.warning("PULSE_RESET_CAPITAL applied (token=%s): capital/ledger/"
                       "reconciliation reset to fresh $%.2f; ALL learning retained.",
                       token, float(self.cfg.starting_capital_usd))
        self._persist()

    def _ensure_report_epoch(self) -> None:
        """Load or backfill the report epoch so published reports scope trading to one era."""
        from engine.pulse.report_epoch import (
            backfill_from_capital_marker,
            load_epoch_file,
            write_epoch_file,
        )
        if self._report_epoch.get("ts"):
            write_epoch_file(self._data_dir, self._report_epoch)
        else:
            loaded = load_epoch_file(self._data_dir)
            if loaded and loaded.get("ts"):
                self._report_epoch = loaded
            else:
                backfill = backfill_from_capital_marker(
                    self._data_dir, starting_capital_usd=float(self.cfg.starting_capital_usd))
                if backfill:
                    self._report_epoch = backfill
                    write_epoch_file(self._data_dir, self._report_epoch)
                    logger.info("report_epoch backfilled from capital-reset marker: %s",
                                backfill.get("utc"))
        self._scope_tradingview_to_epoch()

    def _scope_tradingview_to_epoch(self) -> None:
        """One-time per report epoch: drop pre-reset TradingView intake counters/history."""
        if self.tradingview is None:
            return
        from engine.pulse.report_epoch import epoch_ts
        since = epoch_ts(self._report_epoch)
        if since is None:
            return
        token = str(self._report_epoch.get("token") or "")
        marker = self._data_dir / ".tv_epoch_scoped"
        marker_key = "%.3f:%s" % (since, token)
        if marker.exists():
            try:
                if marker.read_text(encoding="utf-8").strip() == marker_key:
                    return
            except Exception:  # noqa: BLE001
                pass
        kept = self.tradingview.scope_since(since)
        try:
            marker.write_text(marker_key, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        logger.info("tradingview scoped to report epoch %s (token=%s): kept %d event_ids",
                    self._report_epoch.get("utc"), token or "?", kept)

    def _resolve_baseline(self) -> None:
        """Establish the one-time accounting baseline. If a baseline was persisted, keep it. Else,
        if the ledger already holds trades from BEFORE this canonical accounting existed, capture
        them as an explicit legacy bucket so every count still reconciles. Otherwise start clean."""
        if self._baseline is not None and self._baseline.get("captured") is not None:
            self._repair_accounting_drift()
            return
        ls = self.ledger.stats()
        eg = self.ledger.exec_gate_stats()
        if not self.reconciler.has_history and int(ls.get("trades", 0) or 0) > 0:
            self._baseline = capture_baseline(ls, eg)
            logger.info("reconciliation baseline captured (legacy ledger): trades=%d settled=%d "
                        "exec_candidates=%d exec_accepted=%d", self._baseline["trades"],
                        self._baseline["settled"], self._baseline["exec_candidates"],
                        self._baseline["exec_accepted"])
        else:
            self._baseline = empty_baseline()
        self._repair_accounting_drift()

    def _repair_accounting_drift(self) -> None:
        """Heal ledger/lifecycle count skew from a persistence race by absorbing into baseline."""
        from engine.pulse.reconciliation import global_reconciliation, repair_accounting_drift
        lc = self.reconciler.report()
        eg = self.ledger.exec_gate_stats()
        ls = self.ledger.stats()
        if global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=ls,
                                 baseline=self._baseline)["global_reconciled"]:
            return
        repaired, changed = repair_accounting_drift(
            lifecycle=lc, exec_gate=eg, ledger_stats=ls, baseline=self._baseline)
        if not changed:
            return
        self._baseline = repaired
        if global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=ls,
                                 baseline=self._baseline)["global_reconciled"]:
            logger.warning("reconciliation drift absorbed into baseline: trades=%d settled=%d "
                           "exec_candidates=%d exec_accepted=%d",
                           self._baseline["trades"], self._baseline["settled"],
                           self._baseline["exec_candidates"], self._baseline["exec_accepted"])
            self._persist()

    def _maybe_load_lane_offline_prior(self) -> None:
        """Apply offline lane_15m policy prior if present and richer than live policy."""
        if getattr(self, "lane_15m_learner", None) is None:
            return
        prior_path = self._data_dir / "lane_15m_learner_offline_prior.json"
        if not prior_path.exists():
            return
        try:
            data = json.loads(prior_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        pol = data.get("policy") or {}
        if not pol:
            return
        # Only adopt offline policy knobs when live learner has not yet self-adjusted,
        # or when explicitly forced via env.
        force = (os.getenv("PULSE_LANE_OFFLINE_PRIOR", "1") or "1").strip().lower() in (
            "1", "true", "yes", "on")
        live_adj = list(getattr(self.lane_15m_learner, "_adjustments", []) or [])
        if not force and live_adj:
            return
        for k, v in pol.items():
            if hasattr(self.lane_15m_learner.policy, k):
                setattr(self.lane_15m_learner.policy, k, v)
        # Never let offline lane prior undercut the favorites floor.
        try:
            floor = float(os.getenv("PULSE_MIN_ENTRY_PRICE", "0.58") or 0.58)
        except (TypeError, ValueError):
            floor = 0.58
        if float(getattr(self.lane_15m_learner.policy, "min_entry_price", 0) or 0) < floor:
            self.lane_15m_learner.policy.min_entry_price = floor
        sweet_min = float(getattr(self.lane_15m_learner.policy, "sweet_min", 0) or 0)
        if sweet_min < floor:
            self.lane_15m_learner.policy.sweet_min = floor
        logger.info("loaded lane_15m offline prior policy from %s (floor=%.2f)",
                    prior_path.name, floor)

    def _load_state(self) -> None:
        """Restore the paper ledger + calibration from disk so P&L survives restarts."""
        if not self._ledger_path.exists():
            return
        try:
            data = json.loads(self._ledger_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — corrupt state never blocks startup
            logger.warning("could not read prior pulse ledger; starting empty")
            return
        self.ledger.load_state(data)
        # restore the CUMULATIVE lifecycle funnel + gate observations + EV + baseline so the
        # report no longer mixes a per-session funnel with a cross-restart ledger.
        acct = data.get("accounting_state") or {}
        learning_compatible = (
            str(acct.get("directional_learning_version") or "") == DIRECTIONAL_LEARNING_VERSION)
        if learning_compatible:
            self.calib.load_state(data.get("calibration_state") or {})
        else:
            logger.warning("directional learning state reset: incompatible/missing version (have=%s want=%s)",
                           acct.get("directional_learning_version"), DIRECTIONAL_LEARNING_VERSION)
            if getattr(self, "cell_learning", None) is not None:
                self.cell_learning.cells = {}
                self.cell_learning._pending = {}
        self.reconciler.load_state(acct.get("lifecycle") or {})
        self.gate_obs.load_state(acct.get("gate_observations") or {})
        self._tv_edge.load_state(acct.get("tv_edge") or {})
        self._rsi_model.load_state(acct.get("rsi_trend") or {})
        self._rsi_model.canonicalize_storage(self.cfg.tradingview_feature_symbol)
        self._tv_learner.load_state(acct.get("tv_learner") or {})
        from engine.pulse.tradingview import canonical_storage_symbol
        feat_sym = self.cfg.tradingview_feature_symbol
        self._tv_pending = [
            {**row, "symbol": canonical_storage_symbol(row.get("symbol"), feat_sym)}
            for row in (acct.get("tv_pending") or [])
        ]
        if self.edge_signal is not None:
            self.edge_signal.load_state(acct.get("edge_signal") or {})
        if self.cex_lead is not None:
            self.cex_lead.load_state(acct.get("cex_lead") or {})
            self._cex_lead_pending = list(acct.get("cex_lead_pending") or [])
        if learning_compatible:
            self._mkt_bench_pending = list(acct.get("mkt_bench_pending") or [])
            self._mkt_bench_recent = deque(
                (tuple(x) for x in (acct.get("mkt_bench_recent") or [])), maxlen=400)
        self._allowlist_explored = int(acct.get("allowlist_explored", 0) or 0)
        self._allowlist_blocked = int(acct.get("allowlist_blocked", 0) or 0)
        # restore research avoid-rules, but RE-VALIDATE each against current evidence (drops legacy
        # 'direction=', excluded liquidity dims, and any rule no longer confidently losing).
        self._research_avoid = set()
        for k in (acct.get("research_avoid") or []):
            d, _, b = str(k).partition("=")
            b = b.lower()
            if (d in self._RESEARCH_AVOID_DIMS and b
                    and self._research_rule_evidence_backed(d, b)):
                self._research_avoid.add("%s=%s" % (d, b))
        self._research_exploit = set()
        for k in (acct.get("research_exploit") or []):
            d, _, b = str(k).partition("=")
            b = b.lower()
            if d in self._RESEARCH_AVOID_DIMS and b and self._research_exploit_backed(d, b):
                self._research_exploit.add("%s=%s" % (d, b))
        if learning_compatible:
            self.selectivity_evidence.load_state(acct.get("selectivity_evidence") or {})
            self.selectivity_gate.load_state(acct.get("selectivity_gate") or {})
            self.hourly_entry_evidence.load_state(acct.get("hourly_entry_evidence") or {})
            self.hourly_entry_gate.load_state(acct.get("hourly_entry_gate") or {})
            if getattr(self, "p_exec_tune", None) is not None:
                self.p_exec_tune.load_state(acct.get("p_exec_tune") or {})
            if getattr(self, "cell_learning", None) is not None:
                self.cell_learning.load_state(acct.get("cell_learning") or {})
                # Warm-start merge: offline import writes directional_cell_learning.json;
                # if the file has cells missing from ledger (or richer counts), fold them in.
                try:
                    self.cell_learning.merge_from_disk()
                except Exception:  # noqa: BLE001
                    logger.exception("cell learning offline merge_from_disk failed")
            self.pre_trade_evidence.load_state(acct.get("pre_trade_evidence") or {})
            self.pre_trade_gate.load_state(acct.get("pre_trade_gate") or {})
            if getattr(self, "gate_auto_tuner", None) is not None:
                self.gate_auto_tuner.load_state(acct.get("gate_auto_tuner") or {})
            if getattr(self, "lane_15m_learner", None) is not None:
                self.lane_15m_learner.load_state(acct.get("lane_15m_learner") or {})
                try:
                    self._maybe_load_lane_offline_prior()
                except Exception:  # noqa: BLE001
                    logger.exception("lane_15m offline prior load failed")
            if getattr(self, "cross_horizon_learner", None) is not None:
                self.cross_horizon_learner.load_state(acct.get("cross_horizon_learner") or {})
            if getattr(self, "binary_intel", None) is not None:
                self.binary_intel.load_state(acct.get("binary_intel") or {})
            if getattr(self, "sawr", None) is not None:
                self.sawr.load_state(acct.get("sawr") or {})
            if getattr(self, "chronos", None) is not None:
                self.chronos.load_state(acct.get("chronos") or {})
        self.tv_context_gate.load_state(acct.get("tv_context_gate") or {})
        self.tv_down_bias_gate.load_state(acct.get("tv_down_bias_gate") or {})
        self.tv_mtf_gate.load_state(acct.get("tv_mtf_gate") or {})
        self.down_stack.load_state(acct.get("down_stack") or {})
        self.late_window_gate.load_state(acct.get("late_window_gate") or {})
        self.late_window_edge.load_state(acct.get("late_window_edge") or {})
        # one-time bootstrap: if no evidence persisted yet, seed it from the existing settled
        # ledger positions so the gate uses LIVE history immediately (not hard-coded numbers).
        if not self.selectivity_evidence.has_data:
            for pos in self.ledger.positions.values():
                if (pos.status == "settled"
                        and (pos.research or {}).get("strategy_version") == DIRECTIONAL_LEARNING_VERSION):
                    self.selectivity_evidence.record(
                        self._selectivity_tags_from_pos(pos), won=bool(pos.won),
                        pnl=float(pos.pnl_usd or 0.0),
                        ev_after_cost=(pos.research or {}).get("ev_after_cost"),
                        outcome_up=pos.outcome_up)
        if not self.hourly_entry_evidence.has_data:
            from engine.pulse.hourly_entry_timing import (hourly_entry_bucket, hourly_lane_bucket,
                                                           is_hourly_window)
            for pos in self.ledger.positions.values():
                rt = pos.research or {}
                if (pos.status != "settled"
                        or rt.get("strategy_version") != DIRECTIONAL_LEARNING_VERSION):
                    continue
                ws = int(rt.get("window_seconds") or 0)
                if not is_hourly_window(ws):
                    continue
                hb = rt.get("hourly_entry_bucket")
                if not hb:
                    ettc = rt.get("entry_ttc_s")
                    if ettc is not None:
                        hb = hourly_entry_bucket(ws - float(ettc), window_seconds=ws)
                if hb and hb != "na":
                    self.hourly_entry_evidence.record(
                        hourly_lane_bucket(hb, asset=rt.get("asset"), side=pos.side),
                        won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0),
                        ev_after_cost=rt.get("ev_after_cost"))
        if self.grok_predictor is not None:
            self.grok_predictor.load_state(acct.get("grok_predictor") or {})
        if self.grok_analyst is not None:
            self.grok_analyst.load_state(acct.get("grok_analyst") or {})
        if self.grok_decider is not None:
            self.grok_decider.load_state(acct.get("grok_decider") or {})
        if self.grok_news is not None:
            self.grok_news.load_state(acct.get("grok_news") or {})
        if self.llm_council is not None:
            self.llm_council.load_state(acct.get("llm_council") or {})
            # one-time, token-gated reset of members whose underlying signal changed meaning (e.g. a
            # 5m chart switched from a trend alert to a mean-reversion alert) -> grade them fresh.
            if self.cfg.tv_reset_members and self.llm_council.maybe_reset(
                    self.cfg.tv_reset_token, self.cfg.tv_reset_members):
                logger.info("llm_council one-time reset applied token=%s members=%s",
                            self.cfg.tv_reset_token, list(self.cfg.tv_reset_members))
        self._council_pending = list(acct.get("council_pending") or [])
        self._grok_pending = list(acct.get("grok_pending") or [])
        self._verifier_pending = list(acct.get("verifier_pending") or [])
        self._recent_windows = list(acct.get("recent_windows") or [])
        self.lessons.load_state(acct.get("lessons") or {})
        self.trade_history.load_state(acct.get("trade_history") or {})
        if not self.trade_history.recent(1):
            self.trade_history.backfill_from_positions(list(self.ledger.positions.values()))
        if self.verifier is not None:
            self.verifier.load_state(acct.get("verifier") or {})
        if self.research_loop is not None:
            self.research_loop.load_state(acct.get("research_loop") or {})
        if self.edge_model is not None and learning_compatible:
            self.edge_model.load_state(acct.get("edge_model") or {})
        ev = acct.get("ev") or {}
        self._ev_before_sum = float(ev.get("before_sum", 0.0) or 0.0)
        self._ev_after_sum = float(ev.get("after_sum", 0.0) or 0.0)
        self._ev_n = int(ev.get("n", 0) or 0)
        if acct.get("baseline"):
            self._baseline = acct.get("baseline")
        if acct.get("report_epoch"):
            self._report_epoch = dict(acct.get("report_epoch") or {})
        _opens = (acct.get("open_snapshots") or []) if learning_compatible else []
        if _opens:
            n = self.price.load_open_state(_opens)
            if n:
                logger.info("restored %d open snapshot(s) from disk", n)
        _eth_opens = (acct.get("eth_open_snapshots") or []) if learning_compatible else []
        if _eth_opens and self._eth_price is not None:
            ne = self._eth_price.load_open_state(_eth_opens)
            if ne:
                logger.info("restored %d ETH open snapshot(s) from disk", ne)
        logger.info("pulse state restored: trades=%d settled=%d realized_pnl=%.3f calib_n=%d "
                    "lifecycle_created=%d", self.ledger.trades, self.ledger.settled,
                    self.ledger.realized_pnl, self.calib.n, self.reconciler.created)

    def _archive_prior_state(self) -> None:
        """Fresh-start: move the existing ledger aside so we begin from a clean baseline."""
        try:
            self._ledger_path.rename(
                self._data_dir / f"btc_pulse_ledger.archived_{int(time.time())}.json")
            logger.info("PULSE_FRESH_START set — archived prior ledger, starting fresh")
        except Exception:  # noqa: BLE001
            pass

    # -- one evaluation/trade/settle pass ----------------------------------- #
    def tick(self, now: Optional[float] = None) -> dict:
        now = float(now if now is not None else time.time())
        self.ticks += 1
        self.last_tick_ts = now
        self.loops.beat("heartbeat", now)      # liveness watchdog: main loop alive
        self.loops.beat("data_ingestion", now)
        self.price.poll(now)               # oracle: RTDS Chainlink ref price
        if self._eth_price is not None:
            self._eth_price.poll(now)      # ETH directional oracle (Chainlink eth/usd ref price)
        if self._btc_hourly_price is not None:
            self._btc_hourly_price.poll(now)  # Binance BTCUSDT: authoritative hourly source
        if self._eth_hourly_price is not None:
            self._eth_hourly_price.poll(now)  # Binance ETHUSDT: authoritative hourly source
        self.leads.poll(now)               # lead predictors (Binance/Coinbase) — features only
        if self.edge_signal is not None:   # feed the OBSERVE-ONLY CEX basket (lead feeds + extras)
            latest = getattr(self.leads, "_latest", {}) or {}
            prices = {"binance_btcusdt": ((latest.get("binance_btcusdt") or (None,))[0], "no_data"),
                      "coinbase_btcusd": ((latest.get("coinbase_btcusd") or (None,))[0], "no_data")}
            for name, fetch in self._cex_extra.items():
                try:
                    px = fetch()
                except Exception:  # noqa: BLE001 — an extra CEX feed never breaks a tick
                    px = None
                prices[name] = (px, "fetch_failed" if px is None else None)
            if not self._cex_extra:            # extras disabled -> mark missing reason
                for nm in ("kraken_btcusd", "bitstamp_btcusd"):
                    if nm in self.edge_signal.basket.buf:
                        prices[nm] = (None, "disabled_by_config")
            self.edge_signal.observe_prices(prices, now)
        if self.research is not None:
            self.research.observe_oracle(self.price.current())
        if self.signals is not None:
            self.signals.observe_price(self.price.current(), now)
        windows = self._directional_windows(now)
        keep_keys = {w.event_id for w in windows} | set(self.ledger.positions)
        self.price.prune_opens(keep_keys)
        if self._eth_price is not None:
            self._eth_price.prune_opens(keep_keys)
        if self._btc_hourly_price is not None:
            self._btc_hourly_price.prune_opens(keep_keys)
        if self._eth_hourly_price is not None:
            self._eth_hourly_price.prune_opens(keep_keys)
        reasons: dict = {}
        evald = []
        # OBSERVE-ONLY external signal (TradingView): drain freshly-received alerts and compute the
        # latest signal feature for this tick. NEVER used by decide()/evaluate_execution().
        tv_feature = None
        if self.tradingview is not None:
            px_now = self.price.current()
            for ev in self.tradingview.drain_pending():   # build the per-symbol RSI alert history
                store_sym = self.tradingview._storage_symbol(ev.symbol)
                self._rsi_model.observe(symbol=store_sym, direction=ev.direction,
                                        ts=(ev.bar_time or ev.received_at))
                # B: ask Grok (async, off hot path) for P(up) given this signal + BTC context
                if self.grok_predictor is not None:
                    self.grok_predictor.request(ev.event_id, {
                        "signal": {"direction": ev.direction, "strength": ev.strength,
                                   "signal_level": ev.signal_level,
                                   "indicator": ev.indicator_name, "symbol": ev.symbol,
                                   "timeframe": ev.timeframe},
                        "btc_price": px_now, "sigma_per_sec": self.price.sigma_per_sec(now),
                        "regime": (self.overlay.current(now).get("regime")
                                   if self.overlay is not None else None),
                        "horizon_s": self.cfg.tradingview_signal_horizon_s})
                # schedule a forward-return eval for EVERY signal (traded or not) so the prediction
                # is built from the full signal history, not only windows the bot traded.
                px_sig = self._tv_oracle_price(store_sym, now)
                if px_sig is not None:
                    self._tv_pending.append({
                        "symbol": store_sym, "direction": ev.direction, "event_id": ev.event_id,
                        "state": self._rsi_model.trend(store_sym).get("state"),
                        "model_pred": self._rsi_model.predict(store_sym).get("prediction"),
                        "price0": float(px_sig),
                        "due_ts": float(ev.bar_time or ev.received_at)
                        + self.cfg.tradingview_signal_horizon_s})
            self._evaluate_tv_forward_returns(now)
            feat = self.tradingview.latest_feature(now=now,
                                                   symbol=self.cfg.tradingview_feature_symbol)
            self.loops.beat("tradingview", now)
            if feat is not None and (feat.get("age_s") is None
                                     or feat["age_s"] <= self.cfg.tradingview_signal_max_feature_age_s):
                tv_feature = feat
                # attach Grok's observe-only P(up) for this signal if it has answered (fail-open)
                if self.grok_predictor is not None:
                    gp = self.grok_predictor.get(feat.get("event_id"))
                    if gp is not None:
                        tv_feature = {**feat, "grok_p_up": gp.get("p_up")}
        try:                               # PRISM Phase 2: observe-only info completeness (no trade impact)
            self._update_prism_information(now, windows)
        except Exception:  # noqa: BLE001 — observe-only; never break a tick
            pass
        self._grade_grok_decisions(now)   # grade prior Grok decisions vs realized window close
        self._grade_council_decisions(now)  # grade prior LLM-council member views vs realized close
        self._grade_verifier_decisions(now)  # counterfactual grade for vetoed (and shadow) setups
        self._grade_cex_lead(now)         # grade prior CEX-lead signals vs realized window close
        self._grade_market_benchmark(now) # grade model-vs-market accuracy (learning-blend gate)
        ov = self.overlay.current(now) if self.overlay is not None else None
        ov_blackout = bool(ov and ov.get("blackout"))
        ov_vol_mult = float(ov.get("vol_multiplier", 1.0)) if ov else 1.0
        # verifiable stop conditions (agent-independent; refreshed each tick from ledger evidence)
        self.stop_monitor.refresh(
            directional_positions=list(self.ledger.positions.values()),
            directional_stats=self.ledger.stats(),
            starting_capital=self.cfg.starting_capital_usd)
        if getattr(self, "clob_feed", None) and windows:
            _tids = []
            for _w in windows:
                if _w.open_ts <= now < _w.close_ts:
                    _tids.extend([_w.up_token_id, _w.down_token_id])
            self.clob_feed.start_ws_background([t for t in _tids if t])
        _grok_news = ((self.grok_news.latest() if self.grok_news is not None else None) or {})

        def _bump(r):
            reasons[r] = reasons.get(r, 0) + 1

        def _finalize(dr, terminal, *, reason=None, stage=None):
            """Close a candidate in exactly one terminal state — no candidate disappears."""
            dr.finalize(terminal, reason=reason, stage=stage)
            self.reconciler.record(dr)
            evald.append(dr.to_dict())
            # count accepted/rejected outcomes for candidates that carried a TradingView signal
            if dr.external and (dr.external.get("source") == "tradingview"):
                self._tv_learner.record_candidate(dr.external.get("direction"),
                                                  accepted=(terminal == "accepted"))
            _bump(terminal if reason is None else f"{terminal}:{reason}")

        for w in windows:
            # asset-matched price oracle (ETH directional windows -> ETH feed; else BTC oracle)
            _pf = self._price_feed_for(w)
            if (int(getattr(w, "window_seconds", 0) or 0) >= 3600
                    and self.cfg.rtds_enabled
                    and "binance" not in str(getattr(_pf, "source_name", "")).lower()):
                _bump("hourly_resolution_feed_unavailable")
                continue
            # snapshot the open price the moment the window begins
            _pf.snapshot_open(w.event_id, w.open_ts, now=now,
                              window_seconds=int(getattr(w, "window_seconds", 300) or 300))
            if now < w.open_ts:
                _bump("not_open_yet")            # upcoming window — not a candidate yet
                continue
            if self.ledger.has_position(w.event_id):
                _bump("already_positioned")      # existing position — not a NEW candidate
                continue
            # ---- CANDIDATE CREATED (every open, non-positioned window) ----
            s_now = _pf.current()
            sigma = _pf.sigma_per_sec(now)
            snap = _pf.open_snapshot(w.event_id)
            ttc = w.seconds_to_close(now)
            _fair_open = self._directional_fair_anchor(w, snap)
            mc = MarketContext(
                event_id=w.event_id, market_id=w.market_id, title=w.title,
                decision_id=w.event_id,          # canonical id == window key == ledger position key
                asset=self._window_asset(w).upper(),
                series_slug=getattr(w, "series_slug", SERIES_SLUG_5M),
                series_label=getattr(w, "series_label", "5m"),
                window_seconds=int(getattr(w, "window_seconds", 300) or 300),
                open_ts=w.open_ts, close_ts=w.close_ts, ttc_s=ttc,
                oracle_source=str(getattr(_pf, "source_name", "unknown")),
                s_open=_fair_open, s_now=s_now, sigma_per_sec=sigma,
                lead_prices={k: (v[0] if v else None)
                             for k, v in (getattr(self.leads, "_latest", {}) or {}).items()})
            dr = DecisionResult(market_context=mc,
                                candidate=CandidateDecision(None, None, None, 0.0, False, "pending"))
            dr.external = tv_feature          # OBSERVE-ONLY external signal (never trades/sizes)
            # early terminal classifications (each candidate ends classified)
            if ttc <= 0:
                _finalize(dr, "expired", reason="window_closed")
                continue
            if snap is None:
                _finalize(dr, "missing_data", reason="no_open_snapshot")
                continue
            _ws_lag = int(getattr(w, "window_seconds", 300) or 300)
            _max_lag = _pf.effective_max_open_lag(_ws_lag)
            if snap.lag_s > _max_lag:
                _finalize(dr, "skipped", reason="open_snapshot_late")
                continue
            if s_now is None or sigma is None:
                _finalize(dr, "missing_data", reason="no_price_or_vol")
                continue
            # FAIL-CLOSED on a stale oracle: never compute fair value / trade on an aged price.
            if not _pf.is_fresh(self.cfg.price_max_age_s, now):
                _finalize(dr, "skipped", reason="stale_price")
                continue
            if _pf.vol.samples < self.cfg.min_vol_samples \
                    or sigma <= self.cfg.sigma_trust_floor:
                _finalize(dr, "skipped", reason="untrusted_vol")
                continue
            if ov_blackout:
                _finalize(dr, "skipped", reason="grok_event_blackout")
                continue
            self._hydrate_window_books(w)
            mc.poly_yes = w.up_book.mid if w.up_book else None
            mc.best_bid = w.up_book.best_bid if w.up_book else None
            mc.best_ask = w.up_book.best_ask if w.up_book else None
            mc.spread = w.up_book.spread if w.up_book else None
            mc.ask_depth_usd = w.up_book.ask_depth_usd if w.up_book else None
            # directional strategy can be disabled entirely — Loop-Eng scope lock
            if not self.cfg.directional_enabled:
                _finalize(dr, "skipped", reason="directional_disabled")
                continue
            if not self._directional_series_allowed(w):
                _finalize(dr, "skipped", reason="directional_series_not_allowed")
                continue
            if self.stop_monitor.is_halted("directional"):
                _finalize(dr, "skipped", reason="directional_stop_halted")
                continue
            # ---- entry-time features (computed BEFORE the decision so the bot's learned
            #      experience can inform it). These never place/size/bypass a trade themselves. ----
            rfeat = None
            if self.research is not None:
                cex_px = (getattr(self.leads, "_latest", {}) or {}).get(
                    "binance_btcusdt", (None,))[0]
                cex_implied = digital_p_up(cex_px, snap.price, sigma, ttc) if cex_px else None
                poly_yes = w.up_book.mid if w.up_book else None
                divergence = (poly_yes - cex_implied) if (poly_yes is not None
                                                          and cex_implied is not None) else None
                self.research.observe_divergence(divergence, cex_implied)
                rfeat = self.research.evaluate(current_divergence=divergence)
                dr.features = rfeat.to_dict()
                dr.mark("feature_scored")
            if self.signals is not None:
                self.signals.observe_poly(mc.poly_yes, mc.spread, mc.ask_depth_usd, now)
                dr.signals = self.signals.snapshot(ttc_s=ttc, now=now).to_dict()
            fsnap = None
            if self.factors is not None:
                from engine.pulse.factors import compute_factors
                _div = (dr.features or {}).get("divergence") if dr.features else None
                fsnap = compute_factors(
                    poly_yes=mc.poly_yes, spread=mc.spread, ask_depth_usd=mc.ask_depth_usd,
                    bid_depth_usd=(w.up_book.bid_depth_usd if w.up_book else None),
                    ttc_s=ttc, signal=dr.signals, divergence=_div,
                    overlay_regime=((ov or {}).get("regime") if ov else None))
                self.factors.observe(fsnap)
                dr.factors = fsnap.to_dict()
            cand_state = None
            if self.markov is not None:
                from engine.pulse.markov import classify_state
                from engine.pulse.decisions import RegimeSnapshot
                cand_state = classify_state(
                    hurst_regime=(rfeat.hurst_regime if rfeat else None),
                    signal_direction=(dr.signals or {}).get("direction"),
                    stale_factor=(fsnap.polymarket_stale_factor if fsnap else None),
                    settlement_boundary_risk=(fsnap.settlement_boundary_risk if fsnap else None),
                    spread=mc.spread, ask_depth_usd=mc.ask_depth_usd)
                self.markov.observe(cand_state)
                dr.regime = RegimeSnapshot(
                    state=cand_state, probs=self.markov.state_outputs(cand_state)).to_dict()
            # calibrated edge model: predict from entry-time features (the realized label trains
            # it later — no leakage). Reported via dr.model; used in the decision blend below.
            model_vec = None
            if self.edge_model is not None:
                from engine.pulse.edge_model import extract_features
                model_vec = extract_features(features=dr.features, signals=dr.signals,
                                             factors=dr.factors)
                dr.model = self.edge_model.predict(model_vec)
            # OBSERVE-ONLY BTC Pulse Edge Signal (CEX basket momentum + stale divergence + OB
            # pressure + pulse_edge_score). NEVER used by decide()/evaluate_execution().
            esnap = None
            if self.edge_signal is not None:
                _rv = (dr.features or {}).get("realized_vol") if dr.features else None
                esnap = self.edge_signal.snapshot(
                    now=now, poly_yes=mc.poly_yes, spread=mc.spread,
                    up_book=w.up_book, down_book=w.down_book, ttc_s=ttc,
                    hurst_regime=(rfeat.hurst_regime if rfeat else None), realized_vol=_rv,
                    tv_strength=(tv_feature or {}).get("strength"), size_usd=self.cfg.size_usd)
                dr.edge = esnap.to_dict()
            # ---- digital fair value, then the CLOSED-LOOP LEARNED-EDGE BLEND ----
            # the overlay can only RAISE sigma (>=1.0) -> more conservative P(up)
            fair = digital_p_up(s_now, snap.price, sigma * ov_vol_mult, ttc)
            fair_used = fair
            # ALWAYS grade the model's P(up) vs the MARKET price (poly_yes) per window so the blend
            # can self-gate on out-of-sample market-beating accuracy (independent of whether it's
            # currently active). Leakage-free: snapshot at decision, grade at window close.
            if (self.edge_model is not None and model_vec is not None and fair is not None
                    and mc.poly_yes is not None):
                _mp_grade = self.edge_model.decision_p_up(model_vec)
                if _mp_grade is not None:
                    self._schedule_market_benchmark(mc.decision_id, snap.price, w.close_ts,
                                                    _mp_grade, mc.poly_yes, fair)
            if (fair is not None and self.cfg.learning_enabled and self.edge_model is not None
                    and model_vec is not None):
                w_learn, why = self._learning_weight()
                mp = self.edge_model.decision_p_up(model_vec) if w_learn > 0 else None
                if mp is not None:
                    blended = min(0.99, max(0.01, (1.0 - w_learn) * fair + w_learn * mp))
                    dr.learning = {"applied": True, "weight": round(w_learn, 4),
                                   "digital_p_up": round(fair, 4), "model_p_up": round(mp, 4),
                                   "blended_p_up": round(blended, 4), "reason": why,
                                   "paper_only": True, "gate_still_authoritative": True}
                    fair_used = blended
                else:
                    dr.learning = {"applied": False, "weight": round(w_learn, 4), "reason": why}
            # ---- CEX-LEAD LATENCY EDGE (grade CEX-implied P(up) vs the MARKET price; PAPER ONLY) ----
            # cex_p_up uses the SAME sigma as fair, so its only difference from fair is the price
            # SOURCE (fresh CEX spot vs the bot's RTDS price) -> isolates the lead-lag. Graded vs the
            # realized close every window; in SHADOW it only measures; in GATED a Wilson-PROVEN bucket
            # may PROPOSE a side (still subject to the safety floor + execution gate below).
            cex_lead_drive = None
            if self.cex_lead is not None:
                cex_px_l = (getattr(self.leads, "_latest", {}) or {}).get(
                    "binance_btcusdt", (None,))[0]
                cex_p_up = (digital_p_up(cex_px_l, snap.price, sigma * ov_vol_mult, ttc)
                            if cex_px_l else None)
                # ORDERFLOW microstructure from the observe-only edge snapshot (short-horizon CEX
                # momentum direction, cross-exchange agreement, orderbook pressure) -> confirmation.
                _mom = (esnap.cex_momentum if esnap else {}) or {}
                _basket_dir = _mom.get("basket_direction")
                _agreement = _mom.get("exchange_agreement")
                _ob_imb = ((esnap.orderbook_pressure if esnap else {}) or {}).get("imbalance")
                # TradingView confirmation (direction + strength) — observe-only signal feed
                _tv_dir = (tv_feature or {}).get("direction")
                _tv_str = (tv_feature or {}).get("strength")
                # Grok news/X sentiment (mispricing confirmation via fresh context)
                _news = ((self.grok_news.latest() if self.grok_news is not None else None) or {})
                _news_sent = _news.get("sentiment")
                cl_sig = self.cex_lead.signal(cex_p_up=cex_p_up, poly_yes=mc.poly_yes,
                                              fair=fair_used, ttc_s=ttc, basket_direction=_basket_dir,
                                              exchange_agreement=_agreement, ob_imbalance=_ob_imb,
                                              tv_direction=_tv_dir, tv_strength=_tv_str,
                                              news_sentiment=_news_sent)
                dr.cex_lead = cl_sig
                if cl_sig.get("has_signal"):
                    self._schedule_cex_lead_grade(mc.decision_id, snap.price, w.close_ts, cl_sig)
                cex_lead_drive = self.cex_lead.decide(
                    cex_p_up=cex_p_up, poly_yes=mc.poly_yes, fair=fair_used, ttc_s=ttc,
                    basket_direction=_basket_dir, exchange_agreement=_agreement, ob_imbalance=_ob_imb,
                    tv_direction=_tv_dir, tv_strength=_tv_str, news_sentiment=_news_sent)
            # ---- GROK DECISION ENGINE (observe-only shadow; PAPER ONLY) ----
            # Request one decision per window (async, off the tick loop), record it observe-only, and
            # schedule a grade vs the realized close (traded or not). Never drives side/size.
            grok_dec = None
            grok_size_frac = 1.0
            pre_trade_size_scale = 1.0
            grok_verdict = None
            allowlist_exploration = False
            context_explored = False
            if self.grok_decider is not None:
                self.loops.beat("signal_generation", now)
                _grok_bundle = self._grok_decision_bundle(mc, dr, w, fair_used, ttc, tv_feature)
                _refresh = self._grok_refresh_token(mc.decision_id, _grok_bundle, ttc=ttc,
                                                    window_seconds=int(getattr(w, "window_seconds", 300) or 300))
                from engine.pulse.grok_bundle import (classify_grok_compute_tier,
                                                      compact_bundle_for_light_tier)
                _tier = classify_grok_compute_tier(
                    _grok_bundle, refresh_token=_refresh,
                    tiered_enabled=self.cfg.grok_tiered_compute_enabled,
                    full_divergence_min=self.cfg.grok_tier_full_divergence_min,
                    deep_divergence_min=self.cfg.grok_tier_deep_divergence_min)
                _grok_bundle["grok_compute_tier"] = _tier
                if _tier == "light":
                    _grok_bundle = compact_bundle_for_light_tier(_grok_bundle)
                self.grok_decider.request(
                    mc.decision_id,
                    _grok_bundle,
                    context=self._grok_decision_context(dr.features, cand_state, ttc, fair_used),
                    refresh_token=_refresh)
                grok_dec = self.grok_decider.get(mc.decision_id)
                dr.grok_decision = grok_dec
                if grok_dec is not None:
                    self._schedule_grok_grade(mc.decision_id, snap.price, w.close_ts, grok_dec)
                    if self.verifier is not None:
                        self.loops.beat("verifier", now)
                        self.verifier.request(mc.decision_id, {
                            "decision": {k: grok_dec.get(k) for k in
                                         ("action", "p_up", "confidence", "size_fraction",
                                          "rationale")},
                            "context": grok_dec.get("context"),
                            "payoff": {"up_ask": (w.up_book.best_ask if w.up_book else None),
                                       "down_ask": (w.down_book.best_ask if w.down_book else None),
                                       "min_reward_risk": self.cfg.min_reward_risk},
                            "digital_fair_p_up": fair_used, "poly_yes": mc.poly_yes,
                            # mispricing context for the checker: divergence + the CEX-lead signal +
                            # the proof the bot's model is worse than the market (veto if edge<costs)
                            "fair_minus_poly": (round(float(fair_used) - float(mc.poly_yes), 4)
                                                if (fair_used is not None and mc.poly_yes is not None)
                                                else None),
                            "cex_lead_mispricing": {k: (dr.cex_lead or {}).get(k) for k in
                                                    ("divergence", "side", "confirmed",
                                                     "tv_confirms", "late_decisive")},
                            "edge_signal": {k: (dr.edge or {}).get(k) for k in
                                            ("stale_divergence_class", "pulse_edge_score_bucket",
                                             "ttc_bucket", "cex_agreement_bucket")},
                            "model_vs_market": self._market_benchmark(),
                            "recent_windows": self._recent_windows_view(6),
                            "lessons": self.lessons.recent(10),
                            "view_accuracy": self.grok_decider.report().get("view_accuracy")})
                self._maybe_schedule_verifier_counterfactual(
                    mc, w, snap, grok_dec, acted=False)
            # ---- LLM COUNCIL: blend quant + Grok + Claude directional views into one consensus ----
            # Council is observe-only for trade authority: consensus is graded, not executed as
            # grok_follow. Quant baseline + CEX-lead drive remain the directional entry paths.
            # Pre-trade analysis still scores readiness and tightens council thresholds dynamically.
            pre_trade_size_scale = 1.0
            if (self.llm_council is not None and self.llm_council.enabled
                    and self.grok_decider is not None):
                claude_pu = None
                if self.claude_decider is not None:
                    self.claude_decider.request(mc.decision_id, _grok_bundle, refresh_token=_refresh)
                    _cv = self.claude_decider.get(mc.decision_id)
                    claude_pu = _cv.get("p_up") if _cv else None
                _grok_pu = (float(grok_dec.get("p_up"))
                            if (grok_dec is not None and grok_dec.get("p_up") is not None) else None)
                _council_views = {"quant": (float(fair_used) if fair_used is not None else None),
                                  "grok": _grok_pu, "claude": claude_pu}
                # TradingView: intrahour 15/30/45/55m alerts combine into ONE multi-timeframe
                # AGREEMENT signal (tv_mtf) that VOTES; per-TF slots are graded only. -- confident when the timeframes agree, neutral
                # when they split. Combining correlated trend alerts into a single robust view (rather
                # than 4 separately-weighted noisy votes) is what the forecast-combination literature
                # recommends at small samples. The per-TF signals are still recorded + graded
                # (dashboard/measurement) so we can see which timeframe earns its keep, but they do NOT
                # multiply the council's TV weight.
                _grade_views = dict(_council_views)
                if self.cfg.council_tv_member and self.tradingview is not None:
                    from engine.pulse.tradingview import tv_symbol_for_window
                    _tv_sym = tv_symbol_for_window(w)
                    _lane_tfs = self._tv_mtf_timeframes_for_window(w)
                    _grade_views.update(self._tv_per_tf_views(now, symbol=_tv_sym, tfs=_lane_tfs))
                    _mtf = self._tv_mtf_view(now, symbol=_tv_sym, tfs=_lane_tfs)
                    if _mtf is not None:
                        _council_views["tv_mtf"] = _mtf               # the single TV voter
                        _grade_views["tv_mtf"] = _mtf
                    if self.cfg.tv_2h_council_grade:
                        _tv2h = self._tv_2h_trend_view(now, symbol=_tv_sym)
                        if _tv2h is not None:
                            _grade_views["tv_2h_trend"] = _tv2h
                _eff_margin = float(self.cfg.llm_council_min_margin)
                _eff_agreement = float(self.cfg.llm_council_min_agreement)
                _pta_preview = None
                if self.cfg.pre_trade_analysis_enabled:
                    from engine.pulse.pre_trade_analysis import dynamic_council_thresholds
                    _pta_preview = self._run_pre_trade_analysis(
                        dr=dr, w=w, mc=mc, fair_used=fair_used, ttc=ttc, now=now,
                        esnap=esnap, council_views=_council_views)
                    _thr = dynamic_council_thresholds(
                        _pta_preview,
                        base_margin=self.cfg.llm_council_min_margin,
                        base_agreement=self.cfg.llm_council_min_agreement,
                        margin_boost_max=self.cfg.pre_trade_margin_boost_max,
                        agreement_boost_max=self.cfg.pre_trade_agreement_boost_max)
                    _eff_margin = float(_thr["effective_margin"])
                    _eff_agreement = float(_thr["effective_agreement"])
                    dr.pre_trade_thresholds = _thr
                council_dec = self.llm_council.decide(
                    _council_views, min_margin=_eff_margin, min_agreement=_eff_agreement)
                dr.council = council_dec
                self._schedule_council_grade(mc.decision_id, snap.price, w.close_ts, _grade_views)
                _cp = council_dec.get("consensus_p_up")
                _side_c = None
                if (self.cfg.council_best_ev and _cp is not None
                        and int(council_dec.get("n_members") or 0) >= self.cfg.llm_council_min_members):
                    # BEST-EV side selection: pick the side with max (P(side) - ask), not the favorite
                    # by probability. This takes the CHEAP underdog when it's underpriced (great
                    # reward/risk, clears the price gates) and skips overpaying for the favorite.
                    _cp = float(_cp)
                    from engine.pulse.llm_council import best_ev_side
                    up_ask = float(w.up_book.best_ask) if (w.up_book and w.up_book.best_ask) else None
                    down_ask = (float(w.down_book.best_ask)
                                if (w.down_book and w.down_book.best_ask) else None)
                    best_side, best_ev = best_ev_side(_cp, up_ask, down_ask,
                                                      min_edge=float(self.cfg.min_edge))
                    if best_side is not None:
                        _side_c = best_side
                        dr.council = {**council_dec, "best_ev_side": best_side,
                                      "best_ev": best_ev, "mode": "best_ev"}
                elif council_dec.get("trade"):
                    _side_c = council_dec["side"]
                if _side_c is not None and _cp is not None:
                    _cp = float(_cp)
                    _book_c = w.up_book if _side_c == "up" else w.down_book
                    _ask_c = float(_book_c.best_ask) if (_book_c and _book_c.best_ask) else None
                    _p_win_c = _cp if _side_c == "up" else (1.0 - _cp)
                    _cex_ok, _cex_reason = self._follow_executable_edge_ok(
                        p_win=_p_win_c, ask=_ask_c)
                    if not _cex_ok:
                        dr.council = {**council_dec, "executable_reject": _cex_reason}
                        _side_c = None
                    if _side_c is not None and self.cfg.pre_trade_analysis_enabled:
                        from engine.pulse.pre_trade_analysis import readiness_bucket
                        _pta = self._run_pre_trade_analysis(
                            dr=dr, w=w, mc=mc, fair_used=fair_used, ttc=ttc, now=now,
                            esnap=esnap, council_views=_council_views,
                            proposed_side=_side_c, proposed_p_up=_cp)
                        if _pta_preview is not None:
                            _pta["preview_score"] = _pta_preview.get("score")
                        _pg = self.pre_trade_gate.evaluate(
                            _pta, evidence=self.pre_trade_evidence)
                        dr.pre_trade = {**_pta, "gate": _pg}
                        if _pg["decision"] == "reject":
                            dr.council = {**dr.council, "pre_trade_blocked": True,
                                          "pre_trade_reason": _pg["reasons"]}
                            _side_c = None
                        else:
                            pre_trade_size_scale = float(_pg.get("size_scale") or 1.0)
                            dr.pre_trade["readiness_bucket"] = readiness_bucket(_pta.get("score"))
            # ---- PRISM ensemble edge E + confidence C (Phase 4, observe-only) ----
            # Computed for every directional candidate (BEFORE the Osmani skip) so the status API
            # surfaces prism_ensemble on the LIVE bot; feeds R = I*max(0,E)*C into the stopping gate
            # (which only restricts on the legacy path). Never authorizes a fill. PAPER ONLY.
            if self.cfg.prism_enabled and s_now is not None and snap is not None and sigma:
                try:
                    from engine.pulse.prism.ensemble_mc import EnsembleInput, run_ensemble
                    _tv_dir = (tv_feature or {}).get("direction")
                    _tv_str = float((tv_feature or {}).get("strength") or 0.5)
                    _tv_score = ((1.0 if _tv_dir == "UP" else (-1.0 if _tv_dir == "DOWN" else 0.0))
                                 * _tv_str)
                    _e_inp = EnsembleInput(
                        s_now=float(s_now), s_open=float(snap.price),
                        sigma_per_sec=float(sigma) * float(ov_vol_mult), ttc_s=float(ttc),
                        ask_up=(w.up_book.best_ask if w.up_book is not None else None),
                        ask_down=(w.down_book.best_ask if w.down_book is not None else None),
                        side=("down" if self.cfg.directional_down_only else None),
                        tv_score_normalized=_tv_score, cex_drift_bps=0.0, markov_state=cand_state)
                    _e_res = run_ensemble(_e_inp, n_paths=int(self.cfg.prism_mc_paths),
                                          tv_drift_scale=float(self.cfg.prism_tv_drift_scale))
                    # Phase 5: temper the MC confidence by the Thompson bucket posterior.
                    _tv_pat = "single" if tv_feature else "none"
                    _tk = self.prism_thompson.key_from_trade({
                        "series_label": mc.series_label, "markov_state": cand_state,
                        "seconds_since_open_at_entry": w.seconds_since_open(now),
                        "prism_tv_pattern": _tv_pat})
                    _tf = self.prism_thompson.thompson_confidence_factor(_tk)
                    _C_final = _e_res.C * _tf
                    dr.prism_ensemble = {**_e_res.to_dict(),
                                         "C_thompson": round(_tf, 4),
                                         "C_final": round(_C_final, 5),
                                         "bucket": _tk.as_str()}
                    _I_now = float((self._prism_info_report or {}).get("I") or 0.0)
                    _R_now = _I_now * max(0.0, _e_res.E) * _C_final
                    dr.prism_ensemble["R"] = round(_R_now, 5)
                    dr.prism_ensemble["I"] = round(_I_now, 4)
                    # ---- Phase 6: cross-asset lead-lag prior (observe-only) ----
                    _asset = _tk.asset
                    _cross_p = None
                    if self.cfg.prism_cross_asset_enabled and self._prism_leader_p:
                        from engine.pulse.prism.cross_asset import apply_cross_asset_prior
                        _leaders = {a: p for a, p in self._prism_leader_p.items() if a != _asset}
                        _cross_p = apply_cross_asset_prior(
                            (fair_used if fair_used is not None else 0.5), _leaders, _asset)
                    self._prism_leader_p[_asset] = _e_res.p_up_mean
                    # ---- Phase 6: agent classify + sizing (observe-only) ----
                    from engine.pulse.prism.agents import AgentKind, classify_agent
                    _agent = classify_agent(_R_now, _I_now, _C_final, self._prism_agent_cfg)
                    _a_side = ("down" if self.cfg.directional_down_only else (_e_res.side or "up"))
                    _a_book = w.up_book if _a_side == "up" else w.down_book
                    _a_ask = _a_book.best_ask if _a_book is not None else None
                    _a_depth = _a_book.ask_depth_usd if _a_book is not None else None
                    _a_pwin = (_e_res.p_up_mean if _a_side == "up" else (1.0 - _e_res.p_up_mean))
                    _sz = self.prism_agents.size_usd(
                        _agent, _R_now, _C_final, _a_ask, _a_depth,
                        thompson_mult=_tf, open_corr=0.0, p_win=_a_pwin)
                    dr.prism_sizing = {**_sz.to_dict(), "R": round(_R_now, 5),
                                       "I": round(_I_now, 4), "C": round(_C_final, 5)}
                    self._prism_agent_report = {
                        "enabled": True, "window": mc.series_label, "agent": _agent.value,
                        "size_usd": round(_sz.size_usd, 4), "R": round(_R_now, 5),
                        "cross_asset_prior": (round(_cross_p, 5) if _cross_p is not None else None),
                        "caps": _sz.caps_applied}
                    self._prism_ensemble_report = {
                        "enabled": True, "window": mc.series_label,
                        "E": round(_e_res.E, 5), "C": round(_e_res.C, 5),
                        "C_final": round(_C_final, 5), "I": round(_I_now, 4),
                        "R": round(_R_now, 5), "agent": _agent.value,
                        "p_up_mean": round(_e_res.p_up_mean, 5), "side": _e_res.side,
                        "bucket": _tk.as_str(), "used_numpy": _e_res.used_numpy}
                except Exception:  # noqa: BLE001 — observe-only; never break a tick
                    pass
            if self._directional_trade_authority_osmani(w):
                self._maybe_schedule_verifier_counterfactual(mc, w, snap, grok_dec, acted=False)
                _finalize(dr, "skipped", reason="osmani_lane_authority")
                continue
            # ---- PRISM optimal stopping (Phase 3, restrict-only; LEGACY directional path only) ----
            # Only reached when Osmani is NOT the directional authority (the block above skipped that
            # case), so the live Osmani soak is untouched. ENTER passes through to the existing gates
            # + execution floor; WAIT/SKIP reject the candidate at stage 'prism_stopping'. E is a
            # Phase-3 placeholder (0.0) — the ensemble edge arrives in Phase 4 — so the safe default is
            # WAIT. Default OFF (PULSE_PRISM_STOPPING_ENABLED). PAPER ONLY.
            if self.prism_stopping is not None and self.cfg.prism_stopping_enabled:
                from engine.pulse.prism.stopping import StoppingDecision
                _ps = None
                _ps_side = ("down" if self.cfg.directional_down_only
                            else ("up" if (fair_used if fair_used is not None else 0.5) >= 0.5
                                  else "down"))
                try:
                    _ps_book = w.up_book if _ps_side == "up" else w.down_book
                    _ps_ask = _ps_book.best_ask if _ps_book is not None else None
                    # Phase 4: real ensemble E, C from dr.prism_ensemble (computed above); fall back
                    # to the belief-distance C and E=0 if the ensemble is unavailable.
                    _pe = getattr(dr, "prism_ensemble", None) or {}
                    # Prefer the Thompson-tempered C_final (Phase 5), else raw ensemble C, else belief.
                    _ps_C = (float(_pe["C_final"]) if _pe.get("C_final") is not None
                             else (float(_pe["C"]) if _pe.get("C") is not None
                                   else (min(1.0, abs(float(fair_used) - 0.5) * 2.0)
                                         if fair_used is not None else 0.0)))
                    _ps_E = float(_pe.get("E") or 0.0)
                    _ps = self.prism_stopping.evaluate(
                        mc.decision_id, sso=float(w.seconds_since_open(now)), ttc_s=float(ttc),
                        I=float((self._prism_info_report or {}).get("I") or 0.0),
                        E=_ps_E, C=_ps_C, belief_posterior_p=fair_used, ask_price=_ps_ask,
                        side=_ps_side)
                    dr.prism_stopping = _ps.to_dict()
                except Exception:  # noqa: BLE001 — restrict-only; never break a tick
                    _ps = None
                if _ps is not None and _ps.decision != StoppingDecision.ENTER:
                    dr.candidate = CandidateDecision(side=_ps_side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False,
                                                     reason="prism_%s" % _ps.decision.value)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected",
                              reason="prism_%s:%s" % (_ps.decision.value, _ps.reason),
                              stage="prism_stopping")
                    continue
            # ---- PRISM Thompson block gate (Phase 5, restrict-only; LEGACY directional path) ----
            # Reject confidently-losing buckets (Wilson upper < breakeven, n>=20) and, when
            # configured, the operator-blocked BNB asset. Default OFF; inert on the live Osmani path.
            if (self.prism_thompson is not None and self.cfg.prism_thompson_gate_enabled):
                _tside = ("down" if self.cfg.directional_down_only
                          else ("up" if (fair_used if fair_used is not None else 0.5) >= 0.5
                                else "down"))
                try:
                    _tk_g = self.prism_thompson.key_from_trade({
                        "series_label": mc.series_label, "markov_state": cand_state,
                        "seconds_since_open_at_entry": w.seconds_since_open(now),
                        "prism_tv_pattern": ("single" if tv_feature else "none")})
                    _blocked = self.prism_thompson.block_bucket(_tk_g)
                except Exception:  # noqa: BLE001
                    _tk_g, _blocked = None, False
                if _blocked:
                    dr.candidate = CandidateDecision(side=_tside, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason="prism_thompson_block")
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected",
                              reason="prism_thompson_block:%s" % _tk_g.as_str(),
                              stage="prism_thompson")
                    continue
            # ---- PRISM agent gate (Phase 6, restrict-only; LEGACY directional path) ----
            # Reject candidates that classify as NO agent (rank below both tiers). Default OFF; the
            # size override for accepted candidates is applied at the fill site below. Inert live.
            if self.cfg.prism_agent_gate_enabled:
                _agent_v = ((getattr(dr, "prism_sizing", None) or {}).get("agent") or "none")
                if _agent_v == "none":
                    _ag_side = ("down" if self.cfg.directional_down_only else "up")
                    dr.candidate = CandidateDecision(side=_ag_side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason="prism_agent_none")
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason="prism_agent_none", stage="prism_agent")
                    continue
            cex_lead_active = False
            tier_active = False
            tier_size = None
            # ---- DIRECTIONAL TIER ENGINE: the regime-aware directional brain (PAPER ONLY) ----
            # When enabled it OWNS the directional decision: it proposes side + size, reusing the
            # cex-lead bypass so opinion gates are skipped, while calibration + execution_gate + caps
            # below remain the authoritative safety floor. Wait/no-trade -> reject at 'tier_engine'.
            if self.cfg.tier_engine_enabled and self.tier_engine is not None:
                _td = None
                try:
                    _td = self._tier_evaluate(w, mc, snap, s_now, sigma, ov_vol_mult, ttc, now,
                                              cand_state, ov_blackout)
                except Exception:  # noqa: BLE001 — never break a tick; treat as no decision
                    _td = None
                dr.tier = (_td.to_dict() if _td is not None else None)
                if _td is not None:
                    if (self.cfg.cell_learning_phase2_enabled
                            and getattr(w, "directional_lane", False)
                            and self.cell_learning is not None):
                        _td = self._tier_apply_cell_phase2(w, mc, _td, now)
                    self._cell_learning_log_tier(w, mc, _td, now, traded=bool(_td.trade))
                if _td is None or not _td.trade:
                    _tr = ("tier_%s" % _td.reason) if _td is not None else "tier_no_decision"
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=_tr, stage="tier_engine")
                    continue
                _t_side = _td.side
                _t_book = w.up_book if _t_side == "up" else w.down_book
                _t_ask = _t_book.best_ask if _t_book is not None else None
                if _t_ask is None:
                    _finalize(dr, "rejected", reason="no_tradeable_ask", stage="tier_engine")
                    continue
                # Tier selects a candidate but receives NO CEX-lead identity or bypass.  It must
                # pass the unified p_exec, opinion, calibration, selectivity and execution gates.
                cex_lead_active = False
                tier_active = True
                side = _t_side
                entry_mode = "tier_%s" % _td.tier.value
                cex_oprob = float(_td.p_up if side == "up" else (1.0 - _td.p_up))
                tier_size = float(_td.size_usd)
                grok_size_frac = max(0.0, tier_size / max(1e-6, float(self.cfg.size_usd)))
                context_explored = False
                from engine.pulse.strategy import PulseDecision
                d = PulseDecision(trade=True, side=side,
                                  token_id=(w.up_token_id if side == "up" else w.down_token_id),
                                  price=float(_t_ask), fair_p_up=fair_used, edge=float(_td.edge),
                                  reason=entry_mode)
                # 15m lane-local filters (light): side lock + SSO band + chart lean + soft size.
                if (self._is_15m_window(w)
                        and getattr(self, "lane_15m_learner", None) is not None
                        and self.lane_15m_learner.cfg.enabled):
                    _ln = self.lane_15m_learner
                    _sso = float(w.seconds_since_open(now))
                    _ok_t, _why_t = _ln.timing_ok(_sso, float(ttc))
                    if not _ok_t:
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=_why_t, stage="lane_15m")
                        continue
                    _ok_s, _why_s = _ln.filter_side(side)
                    if not _ok_s:
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=_why_s, stage="lane_15m")
                        continue
                    if float(_td.edge) < float(_ln.policy.min_edge):
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason="lane15m_min_edge", stage="lane_15m")
                        continue
                    if float(_t_ask) < float(_ln.policy.min_entry_price):
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason="lane15m_min_entry", stage="lane_15m")
                        continue
                    # Dual-horizon chart lean from 5m bar-close path + RSI overlay soft size.
                    _chart_lean = self._tv_15m_chart_lean_for_window(w)
                    dr.tv_15m_chart_lean = _chart_lean
                    _rsi_ov = self._tv_rsi_overlay_for_window(w, now)
                    dr.tv_rsi_overlay = _rsi_ov
                    _sm = float(_ln.side_size_mult(side))
                    if bool(getattr(self.cfg, "tv_15m_chart_lean_enabled", True)) and \
                            bool(getattr(self.cfg, "tv_15m_chart_lean_size", True)):
                        from engine.pulse.tv_15m_price_path import size_mult_for_lean
                        _cm = size_mult_for_lean(side=side, lean=_chart_lean)
                        _sm *= float(_cm)
                    if bool(getattr(self.cfg, "tv_rsi_overlay_enabled", True)) and \
                            bool(getattr(self.cfg, "tv_rsi_overlay_size", True)) and _rsi_ov:
                        from engine.pulse.tv_rsi_overlay import size_mult_for_rsi_overlay
                        _sm *= float(size_mult_for_rsi_overlay(
                            side=side, overlay=_rsi_ov,
                            aligned_mult=float(getattr(self.cfg, "tv_rsi_overlay_aligned_mult", 1.15)),
                            opposed_mult=float(getattr(self.cfg, "tv_rsi_overlay_opposed_mult", 0.45))))
                    if _sm != 1.0:
                        tier_size = round(float(tier_size) * _sm, 2)
                        grok_size_frac = max(0.0, tier_size / max(1e-6, float(self.cfg.size_usd)))
                    tier_size = min(float(tier_size), float(_ln.policy.max_size_usd))
                    grok_size_frac = max(0.0, tier_size / max(1e-6, float(self.cfg.size_usd)))
                    dr.lane_15m = {
                        "side_mode": _ln.policy.side_mode,
                        "min_sso": _ln.policy.min_sso,
                        "sweet": [_ln.policy.sweet_min, _ln.policy.sweet_max],
                        "min_edge": _ln.policy.min_edge,
                        "chart_lean": _chart_lean,
                        "rsi_overlay": _rsi_ov,
                    }
                # Shared cross-horizon policy (15m↔1h): restrict/size only; never force fills.
                if (getattr(self, "cross_horizon_learner", None) is not None
                        and self.cross_horizon_learner.cfg.enabled):
                    from engine.pulse.cross_horizon_learner import classify_horizon
                    _xh = self.cross_horizon_learner
                    _xh_hz = classify_horizon(
                        window_seconds=getattr(w, "window_seconds", None),
                        series_slug=getattr(w, "series_slug", None),
                        market_series=getattr(w, "series_label", None),
                    )
                    if _xh_hz in ("15m", "1h"):
                        _xh_ws = float(getattr(w, "window_seconds", 0) or (
                            900.0 if _xh_hz == "15m" else 3600.0))
                        _xh_sso = float(w.seconds_since_open(now))
                        _xh_ev = _xh.evaluate_entry(
                            horizon=_xh_hz, side=side, sso=_xh_sso, ttc_s=float(ttc),
                            window_seconds=_xh_ws)
                        dr.cross_horizon = {
                            "horizon": _xh_hz,
                            "decision": _xh_ev.get("decision"),
                            "reason": _xh_ev.get("reason"),
                            "size_mult": _xh_ev.get("size_mult"),
                            "policy": {
                                "h1_min_sso_frac": _xh.policy.h1_min_sso_frac,
                                "h1_prefer_down": _xh.policy.h1_prefer_down,
                                "h1_block_early_up": _xh.policy.h1_block_early_up,
                                "m15_block_early_up": _xh.policy.m15_block_early_up,
                            },
                        }
                        if _xh_ev.get("decision") == "reject":
                            if self.markov is not None:
                                self.markov.record_terminal(state=cand_state, accepted=False)
                            _finalize(dr, "rejected",
                                      reason=str(_xh_ev.get("reason") or "cross_horizon_reject"),
                                      stage="cross_horizon")
                            continue
                        _xh_sm = float(_xh_ev.get("size_mult") or 1.0)
                        if _xh_sm != 1.0:
                            tier_size = round(float(tier_size) * _xh_sm, 2)
                            grok_size_frac = max(
                                0.0, tier_size / max(1e-6, float(self.cfg.size_usd)))
            if tier_active:
                pass
            elif cex_lead_drive is not None:
                # CEX-LEAD DRIVE: a Wilson-PROVEN divergence bucket proposes the side. Opinion gates
                # bypassed (the proven edge owns the direction); the safety floor (selectivity +
                # calibration + EV gate + caps + breaker) below still applies and stays authoritative.
                cex_lead_active = True
                side = cex_lead_drive["side"]
                up_blk, up_blk_reason = self._directional_up_blocked(side)
                if up_blk:
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason=up_blk_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=up_blk_reason, stage="directional")
                    continue
                if side == "up":
                    up_ok, up_reason = self._up_side_tv_bias_ok(
                        tv_feature, ttc_s=ttc, markov_state=cand_state,
                        esnap=esnap, fair_p_up=fair_used, dr=dr, rfeat=rfeat)
                    if not up_ok:
                        dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                         outcome_prob=None, model_edge=0.0,
                                                         tradeable=False, reason=up_reason)
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=up_reason, stage="down_bias_gate")
                        continue
                entry_mode = ("cex_lead_late" if cex_lead_drive.get("late_decisive") else "cex_lead")
                cex_oprob = float(cex_lead_drive["outcome_prob"])
                # D: edge-scaled (fractional-Kelly) sizing for the proven edge, clamped to a sane band
                grok_size_frac = max(0.25, min(self.cfg.cex_lead_max_size_frac,
                                               float(cex_lead_drive.get("size_frac") or 1.0)))
                book = w.up_book if side == "up" else w.down_book
                ask = book.best_ask if book else None
                if ask is None:
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason="no_tradeable_ask")
                    _finalize(dr, "rejected", reason="no_tradeable_ask", stage="cex_lead")
                    continue
                if float(ask) > self.cfg.max_price:
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason="cex_lead_max_price")
                    _finalize(dr, "rejected", reason="cex_lead_max_price", stage="cex_lead")
                    continue
                from engine.pulse.strategy import PulseDecision
                d = PulseDecision(trade=True, side=side,
                                  token_id=(w.up_token_id if side == "up" else w.down_token_id),
                                  price=float(ask), fair_p_up=fair_used, edge=0.0, reason=entry_mode)
                context_explored = False
            else:
                _up_rr = self._reward_risk_floor("up")
                _force_side = ("down" if self.cfg.directional_down_only else None)
                _ws = int(getattr(w, "window_seconds", 300) or 300)
                from engine.pulse.tv_confidence_tier import (
                    params_from_engine_cfg,
                    resolve_tv_entry_params,
                )
                _proposed_side = _force_side or "down"
                _tv_tier_snap = resolve_tv_entry_params(
                    side=_proposed_side,
                    tv_feature=tv_feature,
                    ttc_s=ttc,
                    window_seconds=_ws,
                    base_min_edge=self.cfg.min_edge,
                    base_max_price=self.cfg.max_price,
                    params=params_from_engine_cfg(self.cfg),
                )
                dr.tv_confidence_tier = _tv_tier_snap
                _eff_min_edge = float(_tv_tier_snap.get("min_edge") or self.cfg.min_edge)
                _eff_max_price = float(_tv_tier_snap.get("max_price") or self.cfg.max_price)
                _tier_key = str(_tv_tier_snap.get("tier") or "base")
                self._tv_tier_counts[_tier_key] = self._tv_tier_counts.get(_tier_key, 0) + 1
                d = decide(w, fair_used, now, min_edge=_eff_min_edge,
                           min_seconds_to_close=self.cfg.min_seconds_to_close,
                           min_depth_usd=self.cfg.min_depth_usd,
                           edge_buffer=self.cfg.edge_buffer, max_price=_eff_max_price,
                           min_seconds_since_open=self.cfg.min_seconds_since_open,
                           basis_buffer=self.cfg.basis_buffer,
                           min_reward_risk=self.cfg.min_reward_risk,
                           min_reward_risk_up=_up_rr if _up_rr > float(self.cfg.min_reward_risk or 0)
                           else None,
                           force_side=_force_side)
            if cex_lead_active:
                outcome_prob = cex_oprob               # CEX-lead P(chosen side wins) on a proven bucket
                dr.p_exec = None
            else:
                # Unified p_exec: Grok-MC + digital + mkt, self-tuned by context
                _ask_px = None
                try:
                    _bk = w.up_book if d.side == "up" else w.down_book
                    _ask_px = float(_bk.best_ask) if _bk and _bk.best_ask is not None else None
                except Exception:  # noqa: BLE001
                    _ask_px = d.price
                _asset = self._window_asset(w)
                _ws = int(getattr(w, "window_seconds", 900) or 900)
                _horizon = "1h" if _ws >= 3600 else ("15m" if _ws >= 600 else "5m")
                _sso = float(w.seconds_since_open(now))
                _lead = "none"
                try:
                    cex_px = (getattr(self.leads, "_latest", {}) or {}).get(
                        "binance_btcusdt", (None,))[0]
                    if cex_px and s_now and abs(float(cex_px) - float(s_now)) / float(s_now) > 0.0003:
                        _lead = "cex_diverge"
                    elif cex_px:
                        _lead = "cex_agree"
                except Exception:  # noqa: BLE001
                    pass
                _pe = self._build_p_exec(
                    side=d.side, fair_used=fair_used, poly_yes=mc.poly_yes,
                    vwap=_ask_px if _ask_px is not None else d.price,
                    s_now=float(s_now), s_open=float(snap.price),
                    sigma=float(sigma * ov_vol_mult), ttc=float(ttc),
                    sso=_sso, asset=_asset, horizon=_horizon, lead_state=_lead)
                dr.p_exec = _pe
                if bool(getattr(self.cfg, "p_exec_enabled", True)) and _pe.get("p_exec") is not None:
                    outcome_prob = float(_pe["p_exec"])
                else:
                    outcome_prob = (fair_used if d.side == "up" else (1.0 - fair_used)) \
                        if fair_used is not None else None
                if bool(getattr(self.cfg, "p_exec_enabled", True)) and not _pe.get("allow", True):
                    dr.action = RejectAction(stage="p_exec", reason=_pe.get("allow_reason") or "p_exec_block")
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=_pe.get("allow_reason") or "p_exec_block",
                              stage="p_exec")
                    continue
            dr.candidate = CandidateDecision(side=d.side, fair_p_up=fair_used,
                                             outcome_prob=outcome_prob, model_edge=d.edge,
                                             tradeable=d.trade, reason=d.reason)
            if not d.trade:
                if d.reason == "reward_risk_too_low":
                    self._payoff_guard_counts["rejected_bad_reward_to_risk"] += 1
                elif d.reason == "edge_below_min" and d.price is not None and float(d.price) >= 0.80:
                    self._payoff_guard_counts["rejected_tiny_upside"] += 1
                dr.action = RejectAction(stage="directional", reason=d.reason)
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=d.reason, stage="directional")
                continue
            up_blk, up_reason = self._directional_up_blocked(d.side)
            if up_blk:
                dr.action = RejectAction(stage="directional", reason=up_reason)
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=up_reason, stage="directional")
                continue
            # --- quant OPINION gates (TV-signal / context / late-window / selectivity). These are
            # the quant's directional opinion; in FOLLOW / CEX-LEAD-DRIVE mode the direction is owned
            # by the proven driver so they are bypassed. The deterministic FLOOR (selectivity +
            # calibration + execution-quality gate + caps) below still applies in every mode.
            if not cex_lead_active:
                cohort_ok, cohort_reason = self._baseline_quant_cohort_ok(
                    side=d.side, esnap=esnap, ttc_s=ttc, tv_feature=tv_feature,
                    window_seconds=int(getattr(w, "window_seconds", 300) or 300),
                    ask_price=d.price)
                if not cohort_ok:
                    self._baseline_cohort_gate_counts[cohort_reason] = (
                        self._baseline_cohort_gate_counts.get(cohort_reason, 0) + 1)
                    dr.action = RejectAction(stage="baseline_cohort_gate", reason=cohort_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=cohort_reason, stage="baseline_cohort_gate")
                    continue
            if (self.cfg.directional_up_restrictions_enabled
                    and not cex_lead_active and d.side == "up"
                    and not self._grok_up_side_allowed()):
                dr.action = RejectAction(stage="grok_decider", reason="grok_no_edge_up")
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason="grok_no_edge_up", stage="grok_decider")
                continue
            if (self.cfg.directional_up_restrictions_enabled
                    and not cex_lead_active and d.side == "up"):
                up_tv_ok, up_tv_reason = self._baseline_up_tv_strength_ok(tv_feature)
                if not up_tv_ok:
                    dr.action = RejectAction(stage="directional", reason=up_tv_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=up_tv_reason, stage="directional")
                    continue
            green_path = False
            if not cex_lead_active:
                green_path = self._green_path_active(
                    side=d.side,
                    window_seconds=int(getattr(w, "window_seconds", 300) or 300))
                if green_path:
                    dr.green_path = {
                        "active": True,
                        "skipped": ["tv_signal", "context", "down_bias", "late_window",
                                      "down_tv_dup", "mtf_gate"],
                    }
                elif d.side == "down":
                    down_tv_ok, down_tv_reason = self._baseline_down_tv_context_ok(tv_feature)
                    if not down_tv_ok:
                        dr.action = RejectAction(stage="directional", reason=down_tv_reason)
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=down_tv_reason, stage="directional")
                        continue
                if not green_path:
                    tv_reason = self._tv_signal_gate(tv_feature, d.side)
                    if tv_reason is not None:
                        dr.action = RejectAction(stage="directional", reason=tv_reason)
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=tv_reason, stage="directional")
                        continue
                    ctx_res = self.tv_context_gate.evaluate(
                        volume_state=(tv_feature or {}).get("volume_state"),
                        hurst_regime=(rfeat.hurst_regime if rfeat else None), ttc_s=ttc,
                        liquidation_spike=(tv_feature or {}).get("liquidation_spike"),
                        event_blackout=(tv_feature or {}).get("event_blackout"),
                        grok_event_risk=_grok_news.get("event_risk"))
                    dr.context_gate = {"decision": ctx_res["decision"], "reasons": ctx_res["reasons"]}
                    if ctx_res["decision"] == "block":
                        dr.action = RejectAction(stage="context_gate", reason=ctx_res["reasons"][0])
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=ctx_res["reasons"][0], stage="context_gate")
                        continue
                    context_explored = (ctx_res["decision"] == "explore")
                    db_res = self._down_bias_eval(side=d.side, tv_feature=tv_feature,
                                                  markov_state=cand_state, ttc_s=ttc, esnap=esnap,
                                                  fair_p_up=fair_used,
                                                  zscore_bucket=(rfeat.zscore_bucket if rfeat else None),
                                                  confidence_tier=self._entry_confidence_tier(dr),
                                                  ask_price=d.price)
                    dr.down_bias_gate = {"decision": db_res["decision"], "reasons": db_res["reasons"]}
                    db_block = (db_res["decision"] == "block"
                                or (d.side == "up" and db_res["decision"] == "explore"))
                    if db_block:
                        dr.action = RejectAction(stage="down_bias_gate", reason=db_res["reasons"][0])
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=db_res["reasons"][0], stage="down_bias_gate")
                        continue
                if green_path or not self.cfg.tv_mtf_conflict_gate_enabled:
                    dr.mtf_gate = {
                        "decision": "pass",
                        "reasons": [],
                        "observe_only": True,
                        "tf_confirm": (tv_feature or {}).get("tf_confirm"),
                        "tf_confirm_direction": (tv_feature or {}).get("tf_confirm_direction"),
                        "tf_confirm_mtf": (tv_feature or {}).get("tf_confirm_mtf"),
                        "mtf_timeframes": (tv_feature or {}).get("mtf_timeframes"),
                        "trend_by_tf": (tv_feature or {}).get("trend_by_tf"),
                    }
                else:
                    mtf_res = self.tv_mtf_gate.evaluate(
                        tf_confirm=(tv_feature or {}).get("tf_confirm"),
                        tf_confirm_direction=(tv_feature or {}).get("tf_confirm_direction"),
                        tf_confirm_mtf=(tv_feature or {}).get("tf_confirm_mtf"),
                        mtf_count=(tv_feature or {}).get("mtf_count"),
                        trend_fresh_count=(tv_feature or {}).get("trend_fresh_count"),
                        side=d.side)
                    dr.mtf_gate = {"decision": mtf_res["decision"], "reasons": mtf_res["reasons"],
                                   "tf_confirm": (tv_feature or {}).get("tf_confirm"),
                                   "tf_confirm_direction": (tv_feature or {}).get("tf_confirm_direction"),
                                   "mtf_timeframes": (tv_feature or {}).get("mtf_timeframes"),
                                   "tf_confirm_mtf": (tv_feature or {}).get("tf_confirm_mtf"),
                                   "trend_by_tf": (tv_feature or {}).get("trend_by_tf")}
                    if mtf_res["decision"] == "block":
                        dr.action = RejectAction(stage="mtf_gate", reason=mtf_res["reasons"][0])
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=mtf_res["reasons"][0], stage="mtf_gate")
                        continue
                if green_path:
                    entry_mode = "green_path"
                else:
                    lw_res = self.late_window_gate.evaluate(ttc_s=ttc, p_up=fair_used)
                    dr.late_window = {"decision": lw_res["decision"], "reason": lw_res["reason"],
                                      "conviction": lw_res["conviction"], "late": lw_res["late"],
                                      "high_conviction": lw_res["high_conviction"]}
                    if lw_res["decision"] == "reject":
                        dr.action = RejectAction(stage="late_window_gate", reason=lw_res["reason"])
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=lw_res["reason"], stage="late_window_gate")
                        continue
                    entry_mode = ("late_window" if (lw_res["late"] and lw_res["high_conviction"])
                                  else "standard")
                    if (self.cfg.directional_up_restrictions_enabled
                            and entry_mode == "late_window" and d.side == "up"):
                        dr.action = RejectAction(stage="late_window_gate",
                                                 reason="late_window_up_blocked")
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason="late_window_up_blocked",
                                  stage="late_window_gate")
                        continue
            # SAFETY FLOOR (ALL MODES incl. grok-follow): 1h learned entry-timing gate (intra-hour
            # bucket + min_seconds_since_open floor) then proven-loss selectivity + calibration.
            _ws_entry = int(getattr(w, "window_seconds", 300) or 300)
            _sso_entry = w.seconds_since_open(now)
            from engine.pulse.hourly_entry_timing import hourly_entry_bucket
            _hbucket = hourly_entry_bucket(_sso_entry, window_seconds=_ws_entry)
            _he_res = self.hourly_entry_gate.evaluate(
                window_seconds=_ws_entry, seconds_since_open=_sso_entry,
                asset=self._window_asset(w), side=d.side,
                evidence=self.hourly_entry_evidence)
            dr.hourly_entry = {"decision": _he_res["decision"], "reasons": _he_res["reasons"],
                               "bucket": _he_res.get("bucket"), "seconds_since_open": _sso_entry,
                               "bad_bucket": _he_res.get("bad_bucket")}
            hourly_entry_explored = False
            if _he_res["decision"] == "reject":
                dr.action = RejectAction(stage="hourly_entry_gate", reason=_he_res["reasons"][0])
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=_he_res["reasons"][0], stage="hourly_entry_gate")
                continue
            hourly_entry_explored = (_he_res["decision"] == "explore")
            from engine.pulse.hourly_entry_timing import is_hourly_window, hourly_lane_bucket
            if is_hourly_window(_ws_entry):
                _hc_ok, _hc_reason, _h_lean = self._hourly_chart_lean_entry_ok(
                    w, d.side, now)
                dr.tv_1h_chart_lean = _h_lean
                dr.hourly_chart_lean = {
                    "decision": "pass" if _hc_ok else "reject",
                    "reason": _hc_reason,
                    "trade_lean": (_h_lean or {}).get("trade_lean"),
                    "alignment": (_h_lean or {}).get("alignment"),
                    "short_n": (_h_lean or {}).get("short_n"),
                }
                if not _hc_ok:
                    dr.action = RejectAction(stage="hourly_chart_lean", reason=_hc_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=_hc_reason, stage="hourly_chart_lean")
                    continue
            _tier_snipe = (
                tier_active and str(entry_mode or "").startswith("tier_snipe"))
            if (is_hourly_window(_ws_entry) and self.cfg.tv_strong_fade_enabled
                    and not (self.cfg.tv_strong_fade_exempt_tier_snipe and _tier_snipe)):
                _asset_tv = self._asset_tv_feature(
                    now, getattr(w, "series_slug", mc.series_slug), window=w)
                sf_ok, sf_reason = self._tv_strong_fade_veto_ok(d.side, _asset_tv)
                dr.tv_strong_fade = {
                    "decision": "pass" if sf_ok else "reject",
                    "reason": sf_reason or None,
                    "signal_level": (_asset_tv or {}).get("signal_level"),
                    "symbol": (_asset_tv or {}).get("symbol"),
                    "tier_snipe_exempt": bool(_tier_snipe and self.cfg.tv_strong_fade_exempt_tier_snipe),
                }
                if not sf_ok:
                    self._tv_strong_fade_counts[sf_reason] = (
                        self._tv_strong_fade_counts.get(sf_reason, 0) + 1)
                    dr.action = RejectAction(stage="tv_strong_fade", reason=sf_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=sf_reason, stage="tv_strong_fade")
                    continue
            sel_tags = {
                "market_series": getattr(w, "series_label", mc.series_label),
                "hurst_regime": (rfeat.hurst_regime if rfeat else None),
                "zscore_bucket": (rfeat.zscore_bucket if rfeat else None),
                "ttc_bucket": ttc_bucket(ttc),
                "hourly_entry_bucket": _hbucket,
                "confidence_tier": _confidence_tier((dr.model or {}).get("model_confidence")
                                                    if (dr.model or {}).get("trained")
                                                    else (dr.signals or {}).get("confidence")),
                "spread_bucket": _spread_bucket(mc.spread),
                "depth_bucket": _depth_bucket(mc.ask_depth_usd),
                "markov_state": cand_state,
                "edge_quality_bucket": (fsnap.edge_quality_bucket if fsnap else None),
                "stale_divergence": (esnap.stale_divergence_class if esnap else None),
                "direction": d.side}
            from engine.pulse.selectivity import calibrate_fair, calibrate_chosen_prob
            raw_fp, cal_fp, cal_diag = calibrate_fair(
                fair, sel_tags, self.selectivity_evidence,
                min_samples=self.cfg.calibration_min_samples,
                max_shrink=self.cfg.calibration_max_shrink)
            # de-bias the probability the EV gate will actually use toward the bucket's REALIZED
            # win-rate so the model's over-claimed edge cannot pass the EV floor in proven contexts.
            raw_op, cal_op, op_diag = calibrate_chosen_prob(
                outcome_prob, sel_tags, self.selectivity_evidence,
                min_samples=self.cfg.calibration_min_samples,
                max_shrink=self.cfg.calibration_max_shrink)
            gate_outcome_prob = cal_op if cal_op is not None else outcome_prob
            dr.calibration = {"raw_fair_p_up": raw_fp, "calibrated_fair_p_up": cal_fp,
                              "diag": cal_diag, "raw_outcome_prob": raw_op,
                              "calibrated_outcome_prob": cal_op, "outcome_prob_diag": op_diag}
            if self.cfg.pre_trade_analysis_enabled and dr.pre_trade is None:
                from engine.pulse.pre_trade_analysis import readiness_bucket
                _pta_floor = self._run_pre_trade_analysis(
                    dr=dr, w=w, mc=mc, fair_used=fair_used, ttc=ttc, now=now, esnap=esnap,
                    proposed_side=d.side, proposed_p_up=gate_outcome_prob)
                _pg_floor = self.pre_trade_gate.evaluate(
                    _pta_floor, evidence=self.pre_trade_evidence)
                dr.pre_trade = {**_pta_floor, "gate": _pg_floor,
                                "readiness_bucket": readiness_bucket(_pta_floor.get("score"))}
                if _pg_floor["decision"] == "reject":
                    dr.action = RejectAction(stage="pre_trade_gate",
                                             reason=_pg_floor["reasons"][0])
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=_pg_floor["reasons"][0],
                              stage="pre_trade_gate")
                    continue
                grok_size_frac = max(0.1, min(1.0, grok_size_frac
                                                * float(_pg_floor.get("size_scale") or 1.0)))
            elif dr.pre_trade is not None:
                _pg_done = (dr.pre_trade.get("gate") or {})
                if _pg_done.get("decision") != "reject":
                    grok_size_frac = max(0.1, min(1.0, grok_size_frac
                                                    * float(_pg_done.get("size_scale") or 1.0)))
            # RESEARCH AUTO-APPLY (self-improving loop): hard-block contexts the Claude research loop
            # flagged as proven-losing. Safety-only / more-selective. Exempt the proven CEX-lead edge.
            if self.cfg.research_auto_apply and not cex_lead_active:
                ra_hit = self._research_avoid_hit(sel_tags)
                if ra_hit is not None:
                    reason = "research_avoid:" + ra_hit
                    dr.action = RejectAction(stage="research_avoid", reason=reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=reason, stage="research_avoid")
                    continue
            # DIRECTIONAL ALLOWLIST (de-risk): the directional model is structurally negative-EV in a
            # near-efficient market, so only take a directional trade in a CONFIDENTLY-WINNING bucket
            # (Wilson lower-bound > breakeven, n>=min). Pre-execution BLOCK, not advisory. Driven
            # strategies (grok-follow / cex-lead) are exempt (they have their own proof).
            if (self.cfg.directional_require_winning_bucket and not cex_lead_active
                    and (not self._any_winning_bucket(sel_tags)
                         or not self._directional_market_benchmark_ok())):
                # cold-start carve-out: let a small capped fraction through as EXPLORATION so the
                # bot keeps trading + learning (otherwise it deadlocks — no trades => no bucket can
                # ever be proven-winning => permanent block => looks frozen). The rest stay blocked.
                if self._allowlist_rng.random() >= float(self.cfg.directional_explore_rate):
                    self._allowlist_blocked += 1
                    dr.action = RejectAction(stage="directional_allowlist",
                                             reason="no_proven_winning_bucket")
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason="no_proven_winning_bucket",
                              stage="directional_allowlist")
                    continue
                self._allowlist_explored += 1   # kept active for learning (exploration trade)
                allowlist_exploration = True
            gate_res = self.selectivity_gate.evaluate(sel_tags, self.selectivity_evidence)
            dr.selectivity = {"decision": gate_res["decision"], "reasons": gate_res["reasons"],
                              "bad_buckets": gate_res["bad_buckets"]}
            if gate_res["decision"] == "reject":
                dr.action = RejectAction(stage="selectivity_gate", reason=gate_res["reasons"][0])
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=gate_res["reasons"][0], stage="selectivity_gate")
                continue
            if cex_lead_active:
                gate_decision = ("cex_lead_explored" if gate_res["decision"] == "explore"
                                 else "cex_lead")
            else:
                gate_decision = "explored" if gate_res["decision"] == "explore" else "passed"
            if hourly_entry_explored:
                gate_decision = ("hourly_explored" if gate_decision in ("passed", "explored")
                                 else f"hourly_explored_{gate_decision}")
            # B (EXPLOIT side): size UP a proven-winning research exploit-context (baseline opinion
            # path only; capped). The execution gate + caps below remain authoritative.
            if (not cex_lead_active and self.cfg.research_auto_apply
                    and not self.cfg.research_forbid_size_increase
                    and self._research_exploit_hit(sel_tags)):
                grok_size_frac = min(self.cfg.cex_lead_max_size_frac,
                                     grok_size_frac * self.cfg.research_exploit_size_mult)
                gate_decision = "exploit_" + gate_decision
            elif (self.cfg.research_forbid_size_increase
                  and self._research_exploit_hit(sel_tags)):
                gate_decision = "exploit_blocked_size_" + gate_decision
            # Execution-realistic edge block (Roan Part IV) + margin-based high-entry guard.
            book = w.up_book if d.side == "up" else w.down_book
            from engine.pulse.execution_realistic import (compute_candidate_edge,
                                                          high_entry_margin_reject)
            edge_block = compute_candidate_edge(
                side=d.side, raw_fair_p=raw_fp, calibrated_fair_p=cal_fp,
                market_price=mc.poly_yes, outcome_prob=gate_outcome_prob, book=book,
                size_usd=round(self.cfg.size_usd * grok_size_frac, 2),
                taker_fee_rate=float(getattr(w, "taker_fee_rate", 0.0) or 0.0),
                up_book=w.up_book, down_book=w.down_book)
            dr.execution_realistic = edge_block
            self._exec_realistic_samples.append(edge_block)
            if len(self._exec_realistic_samples) > 200:
                self._exec_realistic_samples = self._exec_realistic_samples[-200:]
            self._last_simplex = edge_block.get("simplex") or {}
            hre = high_entry_margin_reject(
                ask=(book.best_ask if book else d.price),
                calibrated_prob=gate_outcome_prob,
                min_margin=max(0.04, self.cfg.min_edge),
            )
            _tier_snipe = tier_active and str(entry_mode or "").startswith("tier_snipe")
            if hre and not _tier_snipe:
                self._payoff_guard_counts["rejected_high_entry_insufficient_margin"] += 1
                dr.action = RejectAction(stage="execution_realistic", reason=hre)
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=hre, stage="execution_realistic")
                continue
            # STRICT execution-quality gate (AUTHORITATIVE): EV from the live ask-ladder VWAP, using
            # the CALIBRATED probability so the floor reflects realized edge, not the model's claim.
            # Mispricing-follow buys the CEX-indicated (often underdog) side; waive the favourite
            # floor the same way as Wilson-proven cex-lead drive entries.
            _exploration_trade = (
                allowlist_exploration
                or context_explored
                or hourly_entry_explored
                or gate_decision in ("explored", "cex_lead_explored")
                or str(gate_decision).startswith("hourly_explored")
                or str(gate_decision).endswith("_explored")
                or entry_mode == "grok_explore"
            )
            # High-WR: tier reuses cex_lead_active for opinion-gate bypass, but must NOT waive
            # the favorites floor (min_entry_price). Only true underdog drivers may waive.
            _waive_underdog_floor = (
                (cex_lead_active and not tier_active)
                or entry_mode == "mispricing_follow"
                or (_exploration_trade and float(self.cfg.directional_explore_rate) > 0
                    and not tier_active)
            )
            ex = evaluate_execution(
                side=d.side, book=book, outcome_prob=gate_outcome_prob,
                size_usd=round(self.cfg.size_usd * grok_size_frac, 2),
                tick_size=w.tick_size, ttc_s=ttc,
                min_seconds_to_close=self.cfg.min_seconds_to_close,
                max_spread=self.cfg.exec_max_spread, min_depth_usd=self.cfg.min_depth_usd,
                min_order_usd=self.cfg.exec_min_order_usd,
                max_depth_consume_frac=self.cfg.exec_max_depth_consume_frac,
                min_ev_after_slippage=self.cfg.exec_min_ev_after_slippage,
                min_fill_price=(0.0 if _waive_underdog_floor else self.cfg.min_entry_price),
                taker_fee_rate=float(getattr(w, "taker_fee_rate", 0.0) or 0.0),
                now=now, max_book_age_s=self.cfg.exec_max_book_age_s)
            self.ledger.record_exec(ex.accepted, ex.reason)
            # observe what the gate actually SEES (drives the zero-reject diagnostic)
            self.gate_obs.observe(spread=ex.spread, ask_depth_usd=mc.ask_depth_usd,
                                  slippage=ex.slippage, ev_after_slippage=ex.ev_after_slippage,
                                  ttc_s=ttc)
            dr.cost = ExecutionCostEstimate.from_exec_result(ex)
            dr.mark("execution_costed")
            if not ex.accepted:
                if entry_mode == "mispricing_follow":
                    _fk = f"follow_blocked_{ex.reason}"
                    self._mispricing_gate_counts[_fk] = (
                        self._mispricing_gate_counts.get(_fk, 0) + 1)
                dr.action = RejectAction(stage="execution_gate", reason=ex.reason)
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=ex.reason, stage="execution_gate")
                continue
            d.price = ex.fill_price               # paper fill at realistic VWAP price
            from engine.pulse.sizing import sizing_diagnostics_promoted, sizing_diagnostics
            _pwin_sz = (dr.model or {}).get("p_up") or outcome_prob
            if self.cfg.sizing_promotion_gated:
                _sz = sizing_diagnostics_promoted(
                    sel_tags=sel_tags, is_promoted=self._research_exploit_backed,
                    p_win=_pwin_sz, price=ex.fill_price, ev_after_costs=ex.ev_after_slippage,
                    bankroll_usd=self.cfg.sizing_bankroll_usd,
                    hard_cap_usd=self.cfg.sizing_hard_cap_usd,
                    daily_loss_cap_usd=self.cfg.sizing_daily_loss_cap_usd,
                    daily_loss_so_far=self._daily_loss, base_size_usd=self.cfg.size_usd,
                    global_sizing_enabled=self.cfg.sizing_enabled)
            else:
                _sz = sizing_diagnostics(
                    p_win=_pwin_sz, price=ex.fill_price, ev_after_costs=ex.ev_after_slippage,
                    bankroll_usd=self.cfg.sizing_bankroll_usd,
                    hard_cap_usd=self.cfg.sizing_hard_cap_usd,
                    daily_loss_cap_usd=self.cfg.sizing_daily_loss_cap_usd,
                    daily_loss_so_far=self._daily_loss, base_size_usd=self.cfg.size_usd,
                    sizing_enabled=self.cfg.sizing_enabled)
            dr.sizing = _sz
            trade_size = round(float(_sz.get("actual_size_usd") or self.cfg.size_usd)
                               * grok_size_frac, 2)
            # PRISM Phase 6: when the agent gate owns this fill, size with the PRISM allocator
            # (Sniper/Harvester slice + caps). Restrict-only + default OFF -> inert on the live path.
            if self.cfg.prism_agent_gate_enabled:
                _psz = float((getattr(dr, "prism_sizing", None) or {}).get("size_usd") or 0.0)
                if _psz > 0:
                    trade_size = round(_psz, 2)
                    dr.sizing = {**_sz, "prism_agent": dr.prism_sizing.get("agent"),
                                 "prism_override_usd": trade_size}
            # Tier engine owns the size when it drove the decision (fractional-Kelly + tier caps).
            if tier_active and tier_size is not None:
                trade_size = round(float(tier_size), 2)
                dr.sizing = {**(_sz if isinstance(_sz, dict) else {}),
                             "tier": entry_mode, "tier_size_usd": trade_size}
            dir_cap = (float(self.cfg.starting_capital_usd)
                       * float(self.cfg.directional_max_bankroll_frac))
            open_dir = self._directional_open_exposure()
            if open_dir + trade_size > dir_cap + 1e-6:
                dr.action = RejectAction(stage="directional", reason="directional_bankroll_cap")
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason="directional_bankroll_cap", stage="directional")
                continue
            # cross-lane correlated-exposure cap: don't stack this directional bet on top of same-
            # direction exposure already open in another lane on overlapping windows.
            if self.cfg.correlated_exposure_cap_usd > 0:
                corr = self._btc_correlated_exposure(d.side, now)
                if corr + trade_size > self.cfg.correlated_exposure_cap_usd + 1e-6:
                    dr.action = RejectAction(stage="directional", reason="correlated_exposure_cap")
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason="correlated_exposure_cap", stage="directional")
                    continue
            # Freeze the exact probability and fee-adjusted edge that passed execution.  Headline
            # calibration must score the traded forecast, not an earlier raw digital estimate.
            d.fair_p_up = (float(gate_outcome_prob) if d.side == "up"
                           else (1.0 - float(gate_outcome_prob)))
            d.edge = float(ex.ev_after_slippage or 0.0)
            pos = self.ledger.open_position(w, d, now, size_usd=trade_size,
                                            s_open=self._directional_fair_anchor(w, snap) or snap.price,
                                            decision_id=mc.decision_id)
            if pos is None:
                # gate accepted but the paper fill could not be recorded — do NOT claim a trade;
                # classify as skipped so accepted-terminals == paper-fills == ledger-trades.
                dr.action = RejectAction(stage="execution_gate", reason="paper_fill_not_recorded")
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "skipped", reason="paper_fill_not_recorded")
                continue
            pos.fee_rate = float(ex.fee_rate or 0.0)
            pos.entry_fee_usd = round(float(ex.fee_usd or 0.0), 6)
            if tier_active and self.tier_engine is not None and _td is not None:
                self.tier_engine.record_entry(pos.window_key, _td)
            if rfeat is not None:                 # observe-only entry-time tags
                pos.research = {"hurst_regime": rfeat.hurst_regime,
                                "zscore_bucket": rfeat.zscore_bucket,
                                "half_life_bucket": half_life_bucket(rfeat.half_life_s),
                                "ttc_bucket": ttc_bucket(ttc),
                                "edge_quality_bucket": (fsnap.edge_quality_bucket
                                                        if fsnap else "na"),
                                "markov_state": cand_state,
                                "model_features": model_vec,
                                "spread_bucket": _spread_bucket(mc.spread),
                                "depth_bucket": _depth_bucket(mc.ask_depth_usd),
                                    "confidence_tier": _confidence_tier(
                                        (dr.model or {}).get("model_confidence")
                                        if (dr.model or {}).get("trained")
                                        else (dr.signals or {}).get("confidence"))}
            # OBSERVE-ONLY edge-signal entry tags + EV-after-cost (recorded for every trade)
            if pos.research is None:
                pos.research = {}
            pos.research["market_series"] = getattr(w, "series_label", mc.series_label)
            pos.research["series_slug"] = getattr(w, "series_slug", mc.series_slug)
            pos.research["series_label"] = getattr(w, "series_label", mc.series_label)
            pos.research["market_kind"] = getattr(w, "market_kind", "updown")
            pos.research["strike_price"] = getattr(w, "strike_price", None)
            pos.research["directional_slug"] = getattr(w, "slug", "")
            pos.research["asset"] = self._window_asset(w)   # ETH positions settle on the ETH oracle
            pos.research["window_seconds"] = int(getattr(w, "window_seconds", mc.window_seconds) or 300)
            from engine.pulse.directional_labels import directional_trade_labels
            _dl = directional_trade_labels(
                title=str(getattr(w, "title", "") or ""),
                series_label=str(getattr(w, "series_label", mc.series_label) or ""),
                series_slug=str(getattr(w, "series_slug", mc.series_slug) or ""),
                slug=str(getattr(w, "slug", "") or ""),
                window_seconds=int(getattr(w, "window_seconds", mc.window_seconds) or 300),
                market_kind=str(getattr(w, "market_kind", "updown") or "updown"),
            )
            pos.research.update(_dl)
            pos.research["ev_after_cost"] = ex.ev_after_slippage
            pos.research["gate_decision"] = gate_decision     # passed | explored (selectivity gate)
            pos.research["context_gate"] = ("explore" if context_explored else "pass")
            # late-window high-conviction tags (for the observe-only time-decay edge measurement)
            from engine.pulse.late_window import conviction_bucket as _conv_bucket
            pos.research["entry_mode"] = entry_mode
            pos.research["strategy_version"] = DIRECTIONAL_LEARNING_VERSION
            if tier_active and _td is not None:
                pos.research["p_tier_up"] = float(_td.p_up)
                pos.research["p_tier_chosen"] = float(
                    _td.p_up if d.side == "up" else (1.0 - _td.p_up))
            pos.research["entry_ttc_s"] = float(ttc)
            pos.research["seconds_since_open_at_entry"] = round(_sso_entry, 1)
            pos.research["hourly_entry_bucket"] = _hbucket
            pos.research["hourly_gate_decision"] = _he_res["decision"]
            if getattr(dr, "p_exec", None):
                _pe = dr.p_exec or {}
                pos.research["p_exec"] = _pe.get("p_exec")
                pos.research["p_blend"] = _pe.get("p_blend")
                pos.research["p_mc"] = _pe.get("p_mc")
                pos.research["p_mkt"] = _pe.get("p_mkt")
                pos.research["p_digital_side"] = _pe.get("p_digital_side")
                pos.research["p_exec_context"] = _pe.get("context_key")
                pos.research["p_exec_weights"] = _pe.get("weights")
                pos.research["dir_mc"] = {
                    k: (_pe.get("mc") or {}).get(k)
                    for k in ("p_mc", "p_mc_adj", "p_digital", "p_crash", "se", "available")
                }
            if getattr(dr, "tv_15m_chart_lean", None):
                pos.research["tv_15m_chart_lean"] = dr.tv_15m_chart_lean
                pos.research["tv_15m_trade_lean"] = (dr.tv_15m_chart_lean or {}).get("trade_lean")
                pos.research["tv_15m_chart_alignment"] = (dr.tv_15m_chart_lean or {}).get("alignment")
                pos.research["tv_15m_short_pattern"] = (dr.tv_15m_chart_lean or {}).get("short_pattern")
                pos.research["tv_15m_regime_pattern"] = (dr.tv_15m_chart_lean or {}).get("regime_pattern")
                _tl = str((dr.tv_15m_chart_lean or {}).get("trade_lean") or "").lower()
                if _tl in ("up", "down") and str(pos.side or "").lower() in ("up", "down"):
                    pos.research["tv_15m_lean_aligned"] = (_tl == str(pos.side).lower())
            if getattr(dr, "tv_1h_chart_lean", None):
                pos.research["tv_1h_chart_lean"] = dr.tv_1h_chart_lean
                pos.research["tv_1h_trade_lean"] = (dr.tv_1h_chart_lean or {}).get("trade_lean")
                pos.research["tv_1h_chart_alignment"] = (dr.tv_1h_chart_lean or {}).get("alignment")
                pos.research["tv_1h_short_pattern"] = (dr.tv_1h_chart_lean or {}).get("short_pattern")
                pos.research["tv_1h_regime_pattern"] = (dr.tv_1h_chart_lean or {}).get("regime_pattern")
                _tl1h = str((dr.tv_1h_chart_lean or {}).get("trade_lean") or "").lower()
                if _tl1h in ("up", "down") and str(pos.side or "").lower() in ("up", "down"):
                    pos.research["tv_1h_lean_aligned"] = (_tl1h == str(pos.side).lower())
            if getattr(dr, "tv_rsi_overlay", None):
                pos.research["tv_rsi_overlay"] = dr.tv_rsi_overlay
                pos.research["tv_rsi_overlay_lean"] = (dr.tv_rsi_overlay or {}).get("lean")
                _rl = str((dr.tv_rsi_overlay or {}).get("lean") or "").lower()
                if _rl in ("up", "down") and str(pos.side or "").lower() in ("up", "down"):
                    pos.research["tv_rsi_overlay_aligned"] = (_rl == str(pos.side).lower())
            # Binary Intel research tags (pre-trade math + universal 5m TV).
            _bi = getattr(dr, "binary_intel", None) or (
                (dr.pre_trade or {}).get("binary_intel") if getattr(dr, "pre_trade", None) else None)
            if _bi:
                tags = (_bi.get("research_tags") or {})
                pos.research["binary_intel_score"] = _bi.get("composite_score") or tags.get("binary_intel_score")
                pos.research["binary_intel_intelligence"] = _bi.get("intelligence_score")
                pos.research["binary_intel_recommendation"] = _bi.get("recommendation")
                pos.research["binary_intel_size_mult"] = _bi.get("size_mult")
                pos.research["binary_intel_z"] = tags.get("binary_intel_z")
                pos.research["binary_intel_rsi_lean"] = tags.get("binary_intel_rsi_lean")
                pos.research["binary_intel_rsi_decision"] = tags.get("binary_intel_rsi_decision")
                pos.research["tv_cross_asset_rsi"] = tags.get("tv_cross_asset_rsi")
                if tags.get("tv_rsi_overlay_aligned") is not None:
                    pos.research["tv_rsi_overlay_aligned"] = tags.get("tv_rsi_overlay_aligned")
                if tags.get("binary_intel_rsi_lean") and not pos.research.get("tv_rsi_overlay_lean"):
                    pos.research["tv_rsi_overlay_lean"] = tags.get("binary_intel_rsi_lean")
            if getattr(dr, "cross_horizon", None):
                pos.research["cross_horizon"] = dr.cross_horizon
                pos.research["cross_horizon_decision"] = (dr.cross_horizon or {}).get("decision")
                pos.research["cross_horizon_size_mult"] = (dr.cross_horizon or {}).get("size_mult")
            if dr.pre_trade is not None:
                pos.research["pre_trade_score"] = dr.pre_trade.get("score")
                pos.research["pre_trade_recommendation"] = dr.pre_trade.get("recommendation")
                pos.research["readiness_bucket"] = dr.pre_trade.get("readiness_bucket")
                pos.research["pre_trade_gate"] = (dr.pre_trade.get("gate") or {}).get("decision")
            pos.research["conviction_bucket"] = _conv_bucket(fair_used)
            if grok_dec is not None:
                pos.research["grok_snapshot"] = {
                    "action": grok_dec.get("action"),
                    "p_up": grok_dec.get("p_up"),
                    "confidence": grok_dec.get("confidence"),
                }
            if self.verifier is not None:
                vv_snap = self.verifier.get(mc.decision_id)
                if vv_snap and not vv_snap.get("pending"):
                    pos.research["verifier_snapshot"] = {
                        "approved": bool(vv_snap.get("approve")),
                        "reason": str(vv_snap.get("reason") or "")[:120],
                    }
            if self.verifier is not None and grok_verdict:
                pos.research["verifier"] = {"approved": True,
                                            "max_size_fraction": grok_verdict.get("max_size_fraction"),
                                            "reason": grok_verdict.get("reason")}
            if esnap is not None:
                pos.research.update({"edge_stale_divergence": esnap.stale_divergence_class,
                                     "edge_ttc_bucket": esnap.ttc_bucket,
                                     "edge_ob_pressure": esnap.orderbook_pressure.get("bucket"),
                                     "edge_score_bucket": esnap.pulse_edge_score_bucket,
                                     "edge_cex_agreement": esnap.cex_agreement_bucket})
            if tv_feature is not None:
                pos.research.update({
                    "tv_signal_level": tv_feature.get("signal_level"),
                    "tv_mtf_alignment": tv_feature.get("mtf_alignment"),
                    "tv_range_state": tv_feature.get("range_state"),
                    "tv_direction": tv_feature.get("direction"),
                    "tv_strength": tv_feature.get("strength"),
                })
            if tier_active and getattr(self, "_last_cell_key", None) is not None:
                _ck = self._last_cell_key
                _ct = getattr(self, "_last_cell_tier", None)
                pos.research["cell_learning_key"] = _ck.as_str()
                pos.research["cell_learning_tier"] = (_ct.tier.value if _ct is not None else None)
                pos.research["cell_learning_side"] = (_ct.side if _ct is not None else None)
                pos.research["cell_learning_edge"] = (float(_ct.edge) if _ct is not None else 0.0)
                pos.research["cell_learning_p_up"] = (float(_ct.p_up) if _ct is not None else 0.5)
                pos.research["cell_minute_band"] = _ck.minute_band
                pos.research["cell_tv_pattern"] = _ck.tv_pattern
                pos.research["cell_ask_band"] = _ck.ask_band
            if tv_feature is not None:            # observe-only external signal present at entry
                _sym = tv_feature.get("symbol")
                _pred = self._rsi_model.predict(_sym) if _sym else {}
                _trend = self._rsi_model.trend(_sym) if _sym else {}
                pos.research["tv_timeframe"] = tv_feature.get("timeframe")
                pos.external = {"source": "tradingview",
                                "direction": tv_feature.get("direction"),
                                "timeframe": tv_feature.get("timeframe"),
                                "tf_confirm": tv_feature.get("tf_confirm"),
                                "tf_confirm_direction": tv_feature.get("tf_confirm_direction"),
                                "symbol": _sym,
                                "indicator_name": tv_feature.get("indicator_name"),
                                "strength": tv_feature.get("strength"),
                                "strength_bucket": tv_feature.get("strength_bucket"),
                                "signal_level": tv_feature.get("signal_level"),
                                "price": tv_feature.get("price"),
                                "ev_after_cost": ex.ev_after_slippage,   # EV after VWAP/slippage
                                # Composite v2 (observe-only)
                                "vwap_state": tv_feature.get("vwap_state"),
                                "bb_state": tv_feature.get("bb_state"),
                                "volume_state": tv_feature.get("volume_state"),
                                "htf_bias": tv_feature.get("htf_bias"),
                                "composite_version": tv_feature.get("composite_version"),
                                # Composite v3 (observe-only)
                                "adx_state": tv_feature.get("adx_state"),
                                "supertrend_direction": tv_feature.get("supertrend_direction"),
                                "candle_pressure": tv_feature.get("candle_pressure"),
                                "range_state": tv_feature.get("range_state"),
                                "mtf_alignment": tv_feature.get("mtf_alignment"),
                                # Composite v4 order-flow / event (observe-only)
                                "cvd_state": tv_feature.get("cvd_state"),
                                "funding_state": tv_feature.get("funding_state"),
                                "liquidation_spike": tv_feature.get("liquidation_spike"),
                                "event_blackout": tv_feature.get("event_blackout"),
                                # RSI alert-history next-window prediction at entry (observe-only,
                                # leakage-free: scored at settlement before counts are updated)
                                "rsi_trend_state": _trend.get("state"),
                                "rsi_predicted_next": _pred.get("prediction"),
                                "rsi_pred_prob_up": _pred.get("prob_up")}
            # the canonical paper fill — set for EVERY accepted trade (independent of EV stats)
            # so reconciler.ledgered == accepted == ledger.trades by construction.
            dr.fill = PaperFill(window_key=w.event_id, side=d.side, fill_price=ex.fill_price,
                                shares=pos.shares, size_usd=pos.size_usd,
                                decision_id=mc.decision_id)
            # EV before (midpoint) vs after (VWAP/slippage) costs — accepted candidates
            if ex.ev_at_mid is not None and ex.ev_after_slippage is not None:
                self._ev_before_sum += ex.ev_at_mid
                self._ev_after_sum += ex.ev_after_slippage
                self._ev_n += 1
            dr.action = TradeAction(side=d.side, token_id=d.token_id, fill_price=ex.fill_price,
                                    size_usd=self.cfg.size_usd, shares=pos.shares)
            if self.markov is not None:
                self.markov.record_terminal(state=cand_state, accepted=True)
            _finalize(dr, "accepted")

        self._settle_due(now)
        self._reasons = reasons
        if evald:                          # rolling window of recent structured DecisionResults
            self._last_eval = (self._last_eval + evald)[-12:]
        self._prune_positions()
        if self.osmani_loop is not None:
            self.osmani_loop.tick_boundary()
        self._persist()
        return {"ticks": self.ticks, "reasons": reasons, "stats": self.ledger.stats()}

    def _settle_due(self, now: float) -> None:
        for pos in list(self.ledger.open_positions()):
            if pos.close_ts > now:
                continue
            # capture the RTDS Chainlink CLOSE snapshot once, the first post-close tick, so the
            # proxy uses a close price near the actual window close (lag-gated).
            if pos.s_close is None:
                px = self._settle_price_feed_for(pos).current()
                if px is not None:
                    pos.s_close = px
                    pos.close_lag_s = max(0.0, now - pos.close_ts)
            # Only an official Polymarket resolution may become a permanent label.  Oracle
            # open/close remains a reconciliation diagnostic; provisional proxy labels previously
            # trained the bot before they could be checked (and were invalid for Binance hourly).
            priority = ("polymarket_resolution",)
            outcome, source = resolve_window(
                pos.market_id, gamma_feed=self._gamma_feed, priority=priority,
                s_open=pos.s_open, s_close=pos.s_close, close_lag_s=pos.close_lag_s,
                proxy_max_close_lag_s=self.cfg.proxy_max_close_lag_s)
            if outcome is None:
                continue                      # not resolvable yet — retry next tick
            # reconciliation: compare the proxy verdict against the official one when both exist
            proxy_up = proxy_outcome(pos.s_open, pos.s_close) \
                if (pos.close_lag_s is not None
                    and pos.close_lag_s <= self.cfg.proxy_max_close_lag_s) else None
            if source == "polymarket_resolution":
                self.ledger.reconcile(proxy_up, outcome)
            self.ledger.settle(pos.window_key, outcome, s_open=pos.s_open, s_close=pos.s_close,
                               source=source)
            # daily-loss tracker for the Kelly diagnostic (reset per UTC day)
            day = int(now // 86400)
            if day != self._daily_key:
                self._daily_key, self._daily_loss = day, 0.0
            if (pos.pnl_usd or 0.0) < 0:
                self._daily_loss += -float(pos.pnl_usd)
            if (pos.research or {}).get("strategy_version") != DIRECTIONAL_LEARNING_VERSION:
                # Settle legacy positions honestly, but never let an incompatible strategy epoch
                # train the corrected model or mutate its gates.
                continue
            self.calib.observe(pos.fair_at_entry, outcome)
            if self.research is not None:                # observe-only grouped PnL/calibration
                rt = pos.research or {}
                self.research.record_settled(
                    regime=rt.get("hurst_regime"), zbucket=rt.get("zscore_bucket"),
                    half_life_bucket=rt.get("half_life_bucket"), ttc_bucket=rt.get("ttc_bucket"),
                    pnl=float(pos.pnl_usd or 0.0), won=bool(pos.won),
                    fair_at_entry=pos.fair_at_entry, outcome_up=outcome)
            if self.factors is not None:
                self.factors.record_settled(bucket=(pos.research or {}).get("edge_quality_bucket"),
                                            pnl=float(pos.pnl_usd or 0.0), won=bool(pos.won))
            if self.markov is not None:
                self.markov.record_resolution(state=(pos.research or {}).get("markov_state"),
                                              outcome_up=outcome)
            if self.edge_model is not None:
                mvec = (pos.research or {}).get("model_features")
                if isinstance(mvec, dict):           # train on entry features + realized outcome
                    self.edge_model.observe_label(mvec, bool(outcome))
            # learning loop: group this settled outcome by every entry-time tag dimension
            rt = pos.research or {}
            tags = {dim: rt.get(dim) for dim in (
                "hurst_regime", "zscore_bucket", "half_life_bucket", "ttc_bucket",
                "edge_quality_bucket", "markov_state", "spread_bucket", "depth_bucket",
                "confidence_tier", "conviction_bucket", "entry_mode")}
            self._groups.record(tags, pnl=float(pos.pnl_usd or 0.0), won=bool(pos.won),
                                 fair_at_entry=pos.fair_at_entry, outcome_up=outcome)
            # PRISM Phase 5 — learn the Thompson bucket posterior from this settled directional
            # trade (observe-only; persists to prism_thompson.json). PAPER ONLY.
            if self.cfg.prism_enabled and getattr(self, "prism_thompson", None) is not None:
                try:
                    _tk = self.prism_thompson.key_from_trade(pos.research or {})
                    self.prism_thompson.record(_tk, won=bool(pos.won),
                                               pnl=float(pos.pnl_usd or 0.0))
                except Exception:  # noqa: BLE001 — observe-only; never break settlement
                    pass
            # Tier engine — grade the regime-conditioned LRs + daily PnL from this settled trade.
            if getattr(self, "tier_engine", None) is not None:
                try:
                    self.tier_engine.record_settled(pos.window_key, won=bool(pos.won),
                                                    pnl_usd=float(pos.pnl_usd or 0.0), now=now)
                except Exception:  # noqa: BLE001 — never break settlement
                    pass
            # Osmani maker loss-streak sizing (paper) — cut size after consecutive losses.
            try:
                _emode = str((pos.research or {}).get("entry_mode") or "")
                if _emode.startswith("osmani") and self.osmani_loop is not None:
                    gen = getattr(self.osmani_loop, "_generator", None)
                    if gen is not None and hasattr(gen, "record_outcome"):
                        gen.record_outcome(bool(pos.won))
            except Exception:  # noqa: BLE001
                pass
            if getattr(self, "cell_learning", None) is not None:
                try:
                    self.cell_learning.record_settled(
                        pos.window_key, won=bool(pos.won), pnl_usd=float(pos.pnl_usd or 0.0),
                        research=pos.research or {})
                except Exception:  # noqa: BLE001 — observe-only; never break settlement
                    pass
            # Evidence-based High-WR scalar auto-tune (min_edge / min_entry / exec EV / SSO / sweet).
            if getattr(self, "gate_auto_tuner", None) is not None:
                try:
                    _slug = str((pos.research or {}).get("series_slug")
                                or getattr(pos, "series_slug", "") or "").lower()
                    _asset = "eth" if ("eth" in _slug or "ethereum" in _slug) else "btc"
                    # Hourly GateAutoTuner only grades non-15m fills (lane learner owns 15m).
                    if "15m" not in _slug and "15m" not in str(
                            (pos.research or {}).get("market_series") or "").lower():
                        self.gate_auto_tuner.record_settled(
                            won=bool(pos.won), pnl_usd=float(pos.pnl_usd or 0.0),
                            entry_price=float(pos.entry_price) if pos.entry_price is not None else None,
                            asset=_asset,
                            entry_ts=float(getattr(pos, "opened_at", None)
                                           or getattr(pos, "entry_ts", None) or now),
                            now=now)
                        self.gate_auto_tuner.maybe_adjust(self)
                except Exception:  # noqa: BLE001 — never break settlement
                    logger.exception("gate_auto_tuner settlement adjust failed")
            # 15m lane strategy learner — rewrite side/timing/sweet/edge from settled 15m fills.
            if getattr(self, "lane_15m_learner", None) is not None:
                try:
                    _rt15 = pos.research or {}
                    _slug15 = str(_rt15.get("series_slug") or "").lower()
                    _ms15 = str(_rt15.get("market_series") or "").lower()
                    _ws15 = int(_rt15.get("window_seconds") or 0)
                    if ("15m" in _slug15 or "15m" in _ms15 or 600 <= _ws15 <= 1200):
                        _asset15 = "eth" if ("eth" in _slug15 or "ethereum" in _slug15) else "btc"
                        _sso15 = _rt15.get("seconds_since_open_at_entry")
                        if _sso15 is None and _rt15.get("entry_ttc_s") is not None and _ws15:
                            _sso15 = float(_ws15) - float(_rt15["entry_ttc_s"])
                        self.lane_15m_learner.record_settled(
                            won=bool(pos.won),
                            pnl_usd=float(pos.pnl_usd or 0.0),
                            side=str(pos.side or ""),
                            entry_price=float(pos.entry_price) if pos.entry_price is not None else None,
                            asset=_asset15,
                            sso=float(_sso15) if _sso15 is not None else None,
                            ttc_s=float(_rt15["entry_ttc_s"]) if _rt15.get("entry_ttc_s") is not None else None,
                            entry_mode=str(_rt15.get("entry_mode") or ""),
                            chart_lean_aligned=_rt15.get("tv_15m_lean_aligned"),
                            chart_alignment=str(_rt15.get("tv_15m_chart_alignment") or "") or None,
                            short_pattern=str(_rt15.get("tv_15m_short_pattern") or "") or None,
                            rsi_overlay_aligned=_rt15.get("tv_rsi_overlay_aligned"),
                            now=now,
                        )
                        self.lane_15m_learner.maybe_adjust()
                except Exception:  # noqa: BLE001 — never break settlement
                    logger.exception("lane_15m_learner settlement adjust failed")
            # Binary Intel — grade math+TV pre-trade scores; emit Grok autopsy + lessons.
            if getattr(self, "binary_intel", None) is not None:
                try:
                    _rtbi = pos.research or {}
                    _slugbi = str(_rtbi.get("series_slug") or "").lower()
                    _wsbi = int(_rtbi.get("window_seconds") or 0)
                    _assetbi = "eth" if ("eth" in _slugbi or "ethereum" in _slugbi) else "btc"
                    if _wsbi >= 3600 or "1h" in _slugbi or "hourly" in _slugbi:
                        _lanebi = "1h"
                    elif _wsbi >= 600 or "15m" in _slugbi:
                        _lanebi = "15m"
                    else:
                        _lanebi = "5m"
                    self.binary_intel.record_settled(
                        won=bool(pos.won),
                        pnl_usd=float(pos.pnl_usd or 0.0),
                        side=str(pos.side or ""),
                        asset=_assetbi,
                        lane=_lanebi,
                        research=_rtbi,
                        now=now,
                        lessons_book=self.lessons,
                    )
                except Exception:  # noqa: BLE001 — never break settlement
                    logger.exception("binary_intel settlement adjust failed")
            # SAWR — Fill-Quality Pareto meta-controller + Beta side affinity.
            if getattr(self, "sawr", None) is not None:
                try:
                    from engine.pulse.sawr_controller import asset_from_research, lane_from_research
                    _rts = pos.research or {}
                    _lane_s = lane_from_research(_rts)
                    _asset_s = asset_from_research(_rts)
                    _model_p = None
                    if pos.side == "up":
                        _model_p = _rts.get("p_win") or _rts.get("model_p_up")
                    elif pos.side == "down":
                        _pu = _rts.get("model_p_up")
                        if _pu is not None:
                            _model_p = 1.0 - float(_pu)
                        elif _rts.get("p_win") is not None:
                            _model_p = float(_rts.get("p_win"))
                    self.sawr.record_settled(
                        won=bool(pos.won),
                        pnl_usd=float(pos.pnl_usd or 0.0),
                        side=str(pos.side or ""),
                        asset=_asset_s,
                        lane=_lane_s,
                        entry_price=float(pos.entry_price) if pos.entry_price is not None else None,
                        model_p_win=float(_model_p) if _model_p is not None else None,
                        market_mid=(float(pos.entry_price) if pos.entry_price is not None else None),
                        now=now,
                    )
                    self.sawr.maybe_adjust(self)
                except Exception:  # noqa: BLE001 — never break settlement
                    logger.exception("sawr settlement adjust failed")
            # Shared 15m↔1h cross-horizon learner — restrict/size overlays from graded settles.
            if getattr(self, "cross_horizon_learner", None) is not None:
                try:
                    from engine.pulse.cross_horizon_learner import classify_horizon
                    _rtx = pos.research or {}
                    _hz = classify_horizon(
                        window_seconds=_rtx.get("window_seconds"),
                        series_slug=_rtx.get("series_slug"),
                        market_series=_rtx.get("market_series"),
                    )
                    if _hz in ("15m", "1h"):
                        _wsx = float(_rtx.get("window_seconds") or (900.0 if _hz == "15m" else 3600.0))
                        _ssox = _rtx.get("seconds_since_open_at_entry")
                        if _ssox is None and _rtx.get("entry_ttc_s") is not None and _wsx:
                            _ssox = float(_wsx) - float(_rtx["entry_ttc_s"])
                        _slugx = str(_rtx.get("series_slug") or "").lower()
                        _assetx = "eth" if ("eth" in _slugx or "ethereum" in _slugx) else "btc"
                        self.cross_horizon_learner.record_settled(
                            won=bool(pos.won),
                            pnl_usd=float(pos.pnl_usd or 0.0),
                            horizon=_hz,
                            side=str(pos.side or ""),
                            entry_price=float(pos.entry_price) if pos.entry_price is not None else None,
                            window_seconds=_wsx,
                            sso=float(_ssox) if _ssox is not None else None,
                            ttc_s=float(_rtx["entry_ttc_s"]) if _rtx.get("entry_ttc_s") is not None else None,
                            entry_mode=str(_rtx.get("entry_mode") or ""),
                            asset=_assetx,
                            chart_alignment=str(
                                _rtx.get("tv_15m_chart_alignment")
                                or _rtx.get("tv_hourly_chart_alignment")
                                or "") or None,
                            now=now,
                        )
                        self.cross_horizon_learner.maybe_adjust(now=now)
                except Exception:  # noqa: BLE001 — never break settlement
                    logger.exception("cross_horizon_learner settlement adjust failed")
            # OBSERVE-ONLY time-decay edge measurement: grade late-window high-conviction trades
            # (cohort vs other) from this live settled trade. Never affects trading.
            self.late_window_edge.record_settled(
                ttc_s=rt.get("entry_ttc_s"), p_up=pos.fair_at_entry, won=bool(pos.won),
                pnl=float(pos.pnl_usd or 0.0), ev_after_cost=rt.get("ev_after_cost"),
                entry_mode=rt.get("entry_mode"))
            # OBSERVE-ONLY: measure whether the TradingView signal at entry predicted this 5-min
            # outcome and whether aligning helped the bot win (computed AFTER the outcome is known).
            self._tv_edge.record(tv=pos.external, traded_side=pos.side, outcome_up=bool(outcome),
                                 won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0))
            # NOTE: the RSI alert-history model now learns from EVERY signal's forward return
            # (see _evaluate_tv_forward_returns), not just traded windows, so we do NOT also score
            # it here (that would double-count traded windows).
            # OBSERVE-ONLY bucketed learning: if this traded window carried a TradingView signal,
            # record win/PnL/EV by every signal + market-context bucket (for promotion diagnostics).
            # OBSERVE-ONLY edge-signal bucketed learning for EVERY settled trade (CEX/stale/OB).
            from engine.pulse.down_stack import classify_down_stack
            rt = pos.research or {}
            ext = pos.external or {}
            stack_bucket = classify_down_stack(
                mtf_alignment=ext.get("mtf_alignment"),
                stale_divergence=rt.get("edge_stale_divergence"),
                ttc_s=rt.get("entry_ttc_s"),
            )
            self.down_stack.record(
                bucket=stack_bucket, won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0),
                entry_price=pos.entry_price)
            if self.edge_signal is not None:
                self.edge_signal.record_settled(
                    {"stale_divergence": rt.get("edge_stale_divergence"),
                     "ttc_bucket": rt.get("edge_ttc_bucket"),
                     "ob_pressure": rt.get("edge_ob_pressure"),
                     "edge_score": rt.get("edge_score_bucket"),
                     "cex_agreement": rt.get("edge_cex_agreement")},
                    won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0),
                    ev_after_cost=rt.get("ev_after_cost"),
                    reconciled=bool(self.reconciler.report().get("reconciled")))
            # Learned Selectivity Gate: feed bucket evidence + per-gate-decision settled stats.
            _sel_tags = self._selectivity_tags_from_pos(pos)
            self.selectivity_evidence.record(
                _sel_tags, won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0),
                ev_after_cost=(pos.research or {}).get("ev_after_cost"), outcome_up=outcome)
            self.selectivity_gate.record_settled((pos.research or {}).get("gate_decision"),
                                                 won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0))
            from engine.pulse.hourly_entry_timing import is_hourly_window
            _rt_h = pos.research or {}
            if is_hourly_window(_rt_h.get("window_seconds")):
                _hb = _rt_h.get("hourly_entry_bucket")
                if _hb and _hb != "na":
                    self.hourly_entry_evidence.record(
                        hourly_lane_bucket(_hb, asset=_rt_h.get("asset"), side=pos.side),
                        won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0),
                        ev_after_cost=_rt_h.get("ev_after_cost"))
                self.hourly_entry_gate.record_settled(
                    _rt_h.get("hourly_gate_decision"), won=bool(pos.won),
                    pnl=float(pos.pnl_usd or 0.0))
            try:
                if getattr(self, "p_exec_tune", None) is not None:
                    _ck = _rt_h.get("p_exec_context")
                    if _ck:
                        self.p_exec_tune.record(
                            str(_ck),
                            won=bool(pos.won),
                            pnl=float(pos.pnl_usd or 0.0),
                            vwap=float(pos.entry_price or 0.0),
                            p_blend=_rt_h.get("p_blend"),
                            p_mkt=_rt_h.get("p_mkt"),
                            p_mc=_rt_h.get("p_mc"),
                            p_digital=_rt_h.get("p_digital_side"),
                        )
            except Exception:  # noqa: BLE001
                logger.exception("p_exec_tune settlement record failed")
            _rb = (_rt_h.get("readiness_bucket") or _rt_h.get("pre_trade_recommendation"))
            if _rb and str(_rb) not in ("na", "None"):
                from engine.pulse.pre_trade_analysis import readiness_bucket
                _bucket = _rb if _rb in ("<0.40", "0.40-0.48", "0.48-0.62", ">=0.62") else (
                    readiness_bucket(_rt_h.get("pre_trade_score")))
                if _bucket and _bucket != "na":
                    self.pre_trade_evidence.record(
                        _bucket, won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0))
            # grade the maker-checker (approved trade outcome) + record lessons from this settlement
            if self.verifier is not None and (pos.research or {}).get("verifier"):
                self.verifier.grade(pos.decision_id or pos.window_key, won=bool(pos.won),
                                    pnl=float(pos.pnl_usd or 0.0), acted=True)
            rt_hist = pos.research or {}
            self.trade_history.record_settled(
                decision_id=pos.decision_id or pos.window_key,
                title=pos.title,
                side=pos.side,
                entry_mode=rt_hist.get("entry_mode") or "unknown",
                entry_price=float(pos.entry_price),
                size_usd=float(pos.size_usd),
                outcome_up=bool(outcome),
                won=bool(pos.won),
                pnl_usd=float(pos.pnl_usd or 0.0),
                research=rt_hist,
                grok=rt_hist.get("grok_snapshot"),
                verifier=rt_hist.get("verifier_snapshot") or rt_hist.get("verifier"),
            )
            self._record_lessons_from_settlement(pos)
            ext = pos.external or {}
            if ext.get("source") == "tradingview":
                rt = pos.research or {}
                self._tv_learner.record_settled(
                    {"direction": ext.get("direction"), "signal_level": ext.get("signal_level"),
                     "strength_bucket": ext.get("strength_bucket"),
                     "indicator_name": ext.get("indicator_name"),
                     "hurst_regime": rt.get("hurst_regime"), "zscore_bucket": rt.get("zscore_bucket"),
                     "ttc_bucket": rt.get("ttc_bucket"), "spread_bucket": rt.get("spread_bucket"),
                     "depth_bucket": rt.get("depth_bucket"),
                     "vwap_state": ext.get("vwap_state"), "bb_state": ext.get("bb_state"),
                     "volume_state": ext.get("volume_state"), "htf_bias": ext.get("htf_bias"),
                     "composite_version": ext.get("composite_version"),
                     "adx_state": ext.get("adx_state"),
                     "supertrend_direction": ext.get("supertrend_direction"),
                     "candle_pressure": ext.get("candle_pressure"),
                     "range_state": ext.get("range_state"),
                     "mtf_alignment": ext.get("mtf_alignment"),
                     "cvd_state": ext.get("cvd_state"), "funding_state": ext.get("funding_state"),
                     "liquidation_spike": ext.get("liquidation_spike"),
                     "event_blackout": ext.get("event_blackout")},
                    won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0),
                    ev_after_cost=ext.get("ev_after_cost"),
                    reconciled=bool(self.reconciler.report().get("reconciled")))
            logger.info("pulse settled %s side=%s won=%s pnl=%.3f via=%s",
                        pos.title, pos.side, pos.won, pos.pnl_usd or 0.0, source)

    def _prune_positions(self) -> None:
        if len(self.ledger.positions) <= self.cfg.max_positions_kept:
            return
        settled = [p for p in self.ledger.positions.values() if p.status == "settled"]
        settled.sort(key=lambda p: p.close_ts)
        for p in settled[: len(self.ledger.positions) - self.cfg.max_positions_kept]:
            self.ledger.positions.pop(p.window_key, None)

    # -- persistence -------------------------------------------------------- #
    def readiness(self) -> dict:
        """Success-gate readiness report (report-only). Never claims an 80% bot unless ALL gates
        pass. Inputs come from the reconciled ledger + lifecycle (no unmodeled fill assumptions:
        paper fills use the live ask-ladder VWAP)."""
        from engine.pulse.readiness import readiness_report
        from engine.pulse.reconciliation import global_reconciliation
        ls = self.ledger.stats()
        lc = self.reconciler.report()
        eg = self.ledger.exec_gate_stats()
        cal = self.calib.to_dict()
        gr = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=ls,
                                   baseline=(self._baseline or empty_baseline()))
        recon_ok = bool(gr.get("global_reconciled"))
        # calibration_error gate expects an ECE (<=0.10), NOT the Brier score — pass the model's
        # actual ECE (None if unavailable -> gate stays unmet, which is the honest default).
        model_ece = (self.edge_model.calibration_error() if self.edge_model is not None else None)
        return readiness_report(
            accepted=int(ls.get("settled", 0) or 0), win_rate=ls.get("win_rate"),
            net_pnl=ls.get("realized_pnl_usd"), profit_factor=ls.get("profit_factor"),
            calibration_error=model_ece, max_drawdown=ls.get("max_drawdown_usd"),
            avg_win=ls.get("avg_win_usd"), avg_loss=ls.get("avg_loss_usd"),
            reconciliation_ok=recon_ok, missing_settlement=False, unmodeled_fill=False,
            safety_bypass=False)

    def _meta_learning_status(self) -> dict:
        """Status of the LLM batch meta-learning layer (bundle written; integration availability).
        Never makes live trade decisions."""
        try:
            from engine.pulse.overlay import xai_key_present
            available = bool(xai_key_present())
        except Exception:  # noqa: BLE001
            available = False
        return {"enabled": True, "report_only": True, "no_live_trading_decisions": True,
                "bundle_artifact": "btc_pulse_meta_bundle.json",
                "grok_integration_available": available}

    def _tv_2h_review_for_symbol(self, symbol: str,
                                now: Optional[float] = None) -> dict:
        """2h TV alert trend + price path for one symbol (observe-only)."""
        from engine.pulse.tv_2h_review import compute_tv_2h_review
        if not self.cfg.tv_2h_review_enabled or self.tradingview is None:
            return {"enabled": False, "observe_only": True}
        ts = float(now if now is not None else (self.last_tick_ts or time.time()))
        return compute_tv_2h_review(
            alerts=self.tradingview.alert_history_for_symbol(symbol),
            now=ts,
            lookback_s=self.cfg.tv_2h_lookback_s,
            symbol=symbol,
            oracle_price_now=self._tv_oracle_price(symbol, ts),
        )

    def _tv_2h_review_report(self) -> dict:
        """Status/light-report block for the 2h TV trend review."""
        symbols = ("BTCUSD", "ETHUSD")
        by_symbol = {sym: self._tv_2h_review_for_symbol(sym) for sym in symbols}
        focus = by_symbol.get("BTCUSD") or {}
        return {
            "enabled": bool(self.cfg.tv_2h_review_enabled),
            "observe_only": True,
            "lookback_s": int(self.cfg.tv_2h_lookback_s),
            "pretrade_enabled": bool(self.cfg.tv_2h_review_pretrade),
            "council_grade_enabled": bool(self.cfg.tv_2h_council_grade),
            "focus_symbol": "BTCUSD",
            "by_symbol": by_symbol,
            "focus": focus,
        }

    def _tv_oracle_price(self, symbol: str, now: float) -> Optional[float]:
        """Oracle spot for TV forward-return grading (Chainlink BTC; Binance for USDT lanes)."""
        sym = str(symbol or "").upper()
        if "ETH" in sym:
            try:
                eth = (getattr(self.leads, "_latest", {}) or {}).get("binance_ethusdt", (None,))[0]
                if eth is not None:
                    return float(eth)
            except Exception:  # noqa: BLE001
                pass
        if sym in ("BTCUSDT",) or sym.endswith("BTCUSDT"):
            try:
                btc = (getattr(self.leads, "_latest", {}) or {}).get("binance_btcusdt", (None,))[0]
                if btc is not None:
                    return float(btc)
            except Exception:  # noqa: BLE001
                pass
        return self.price.current()

    def _tv_mtf_timeframes_for_window(self, w) -> tuple[str, ...]:
        """MTF ladder for council/Grok/tier on this window."""
        return tuple(self.cfg.tradingview_mtf_timeframes or ())

    def _tier_tv_tfs_for_window(self, w) -> tuple[str, ...]:
        """TV keys read for tier/cell learning on this window."""
        if getattr(w, "market_kind", "") == "above":
            return self._tv_mtf_timeframes_for_window(w)
        return self._TIER_TV_TFS

    def _evaluate_tv_forward_returns(self, now: float) -> None:
        """Resolve due forward-return evals: compare oracle spot now vs at signal time and teach the
        RSI model whether each signal predicted the move. Observe-only; leakage-free."""
        if not self._tv_pending:
            return
        still = []
        for pend in self._tv_pending:
            if now < pend["due_ts"]:
                still.append(pend)
                continue
            px_now = self._tv_oracle_price(pend.get("symbol"), now)
            if px_now is not None:
                outcome_up = float(px_now) >= float(pend["price0"])
                self._rsi_model.record_signal_outcome(
                    symbol=pend["symbol"], state=pend.get("state"),
                    model_pred=pend.get("model_pred"), signal_direction=pend.get("direction"),
                    outcome_up=outcome_up)
                # B: grade Grok's per-signal P(up) against the same realized move (leakage-free)
                if self.grok_predictor is not None and pend.get("event_id"):
                    self.grok_predictor.score(pend["event_id"], outcome_up)
            elif now <= pend["due_ts"] + 600:    # grace: retry until an oracle price is available
                still.append(pend)
            # else: stale with no price -> drop
        self._tv_pending = still[-1000:]

    @staticmethod
    def _r(v, nd=4):
        """Round floats for a compact, well-typed payload; pass through non-numerics."""
        try:
            return round(float(v), nd) if v is not None else None
        except (TypeError, ValueError):
            return v

    @staticmethod
    def _reward_risk(ask):
        """Binary payoff at ask price p: a win nets (1-p)/p per $; breakeven win-rate ~= p."""
        try:
            p = float(ask)
            if p <= 0 or p >= 1:
                return None
            return {"ask": round(p, 4), "win_payoff_per_$": round((1.0 - p) / p, 4),
                    "breakeven_win_rate": round(p, 4)}
        except (TypeError, ValueError):
            return None

    def _grok_decision_context(self, rf, cand_state, ttc, fair_used) -> dict:
        """Compact entry-time context tags used to bucket the decider's OWN accuracy as it learns."""
        conv = abs(float(fair_used) - 0.5) * 2 if fair_used is not None else None
        conv_bucket = ("na" if conv is None else
                       ("strong" if conv >= 0.4 else ("lean" if conv >= 0.2 else "coinflip")))
        return {"hurst_regime": (rf.get("hurst_regime") if rf else None),
                "markov_state": cand_state, "ttc_bucket": ttc_bucket(ttc),
                "conviction_bucket": conv_bucket}

    def _grok_tv_fingerprint(self, tv_trend: Optional[dict]) -> str:
        """Stable key for MTF changes that should trigger a fresh Grok read."""
        tv_trend = tv_trend or {}
        parts = [str(tv_trend.get("confirm_mtf")), str(tv_trend.get("fresh_tf_count"))]
        for label, row in sorted((tv_trend.get("charts") or {}).items()):
            if not isinstance(row, dict):
                continue
            parts.append("%s:%s:%s:%s" % (
                label, row.get("direction"), row.get("signal_level"), row.get("strength")))
        return "|".join(parts)

    def _grok_refresh_token(self, decision_id: str, bundle: dict, *, ttc: float,
                            window_seconds: int) -> Optional[str]:
        """Return refresh_token when TV MTF flips or 15m window enters baseline entry band."""
        import hashlib
        tv_trend = bundle.get("tradingview_trend") or {}
        fp = self._grok_tv_fingerprint(tv_trend)
        prev = self._grok_tv_fp.get(decision_id)
        tokens: list[str] = []
        if prev is not None and prev != fp:
            tokens.append("tv:" + hashlib.sha256(fp.encode()).hexdigest()[:12])
        ws = int(window_seconds or 300)
        if ws >= 900 and 480.0 <= float(ttc) <= 660.0:
            if decision_id not in self._grok_entry_band_seen:
                self._grok_entry_band_seen.add(decision_id)
                if prev is not None:
                    tokens.append("entry15m")
        self._grok_tv_fp[decision_id] = fp
        return "+".join(tokens) if tokens else None

    def _book_side_snapshot(self, book) -> "dict | None":
        if book is None:
            return None
        return {
            "mid": self._r(book.mid), "spread": self._r(book.spread),
            "best_bid": self._r(book.best_bid), "best_ask": self._r(book.best_ask),
            "bid_depth_usd": self._r(book.bid_depth_usd, 1),
            "ask_depth_usd": self._r(book.ask_depth_usd, 1),
            "ask_levels": len(book.asks or []), "bid_levels": len(book.bids or []),
        }

    def _market_window_snapshot(self, w, *, now=None) -> dict:
        now = now if now is not None else (self.last_tick_ts or time.time())
        return {
            "series_slug": getattr(w, "series_slug", SERIES_SLUG_5M),
            "series_label": getattr(w, "series_label", "5m"),
            "window_seconds": int(getattr(w, "window_seconds", 300) or 300),
            "event_id": w.event_id, "title": w.title,
            "ttc_s": self._r(w.seconds_to_close(now), 1),
            "up": self._book_side_snapshot(w.up_book),
            "down": self._book_side_snapshot(w.down_book),
        }

    def _active_markets_for_grok(self) -> list:
        try:
            windows = self._directional_windows(float(self.last_tick_ts or time.time()))
        except Exception:  # noqa: BLE001
            return []
        out = []
        for win in windows:
            try:
                self._hydrate_window_books(win)
            except Exception:  # noqa: BLE001
                pass
            out.append(self._market_window_snapshot(win))
        return out

    def _cex_prices_snapshot(self) -> dict:
        out = {}
        if self.leads is not None:
            for k, v in (getattr(self.leads, "_latest", {}) or {}).items():
                px = v[0] if isinstance(v, (tuple, list)) else v
                out[k] = self._r(px, 2)
        return out

    def _tv_2h_for_window(self, w, now: Optional[float] = None) -> Optional[dict]:
        """2h TV review for hourly windows only (None for short windows / disabled)."""
        from engine.pulse.hourly_entry_timing import is_hourly_window
        if not self.cfg.tv_2h_review_enabled or self.tradingview is None:
            return None
        ws = int(getattr(w, "window_seconds", 300) or 300)
        if not is_hourly_window(ws):
            return None
        from engine.pulse.tradingview import tv_symbol_for_window
        sym = tv_symbol_for_window(w, default_btc=self.cfg.tradingview_feature_symbol)
        if not sym:
            return None
        return self._tv_2h_review_for_symbol(sym, now)

    def _run_pre_trade_analysis(self, *, dr, w, mc, fair_used, ttc, now, esnap=None,
                                council_views=None, proposed_side=None,
                                proposed_p_up=None) -> dict:
        """Synthesize all entry-time data into one readiness analysis (deterministic)."""
        from engine.pulse.pre_trade_analysis import analyze_pre_trade
        ws = int(getattr(w, "window_seconds", 300) or 300)
        sigma = self.price.sigma_per_sec(now)
        tv_2h = self._tv_2h_for_window(w, now) if self.cfg.tv_2h_review_pretrade else None
        tv_per_tf = None
        if self.tradingview is not None:
            from engine.pulse.tradingview import tv_symbol_for_window
            _tv_sym = tv_symbol_for_window(w, default_btc=self.cfg.tradingview_feature_symbol)
            tv_per_tf = self._tv_per_tf_views(
                now, symbol=_tv_sym, tfs=self._tv_mtf_timeframes_for_window(w))
        analysis = analyze_pre_trade(
            fair_p_up=fair_used,
            poly_yes=mc.poly_yes,
            council_views=council_views,
            proposed_side=proposed_side,
            proposed_p_up=proposed_p_up,
            edge_snap=((esnap.to_dict() if esnap is not None else None) or dr.edge),
            features=dr.features,
            ttc_s=ttc,
            window_seconds=ws,
            seconds_since_open=w.seconds_since_open(now),
            spread=mc.spread,
            ask_depth_usd=mc.ask_depth_usd,
            price_fresh=bool(self.price.is_fresh(self.cfg.price_max_age_s, now)),
            vol_trusted=bool(
                self.price.vol.samples >= self.cfg.min_vol_samples
                and sigma is not None and sigma > self.cfg.sigma_trust_floor),
            up_ask=(w.up_book.best_ask if w.up_book else None),
            down_ask=(w.down_book.best_ask if w.down_book else None),
            min_edge=self.cfg.min_edge,
            hourly_min_minutes=self.cfg.pre_trade_hourly_min_minutes,
            tv_2h_review=tv_2h,
            tv_per_tf_views=tv_per_tf,
        )
        # Binary Intel pre-trade script — math + universal 5m TV for all lanes.
        if getattr(self, "binary_intel", None) is not None and self.cfg.binary_intel_enabled:
            try:
                feed = self._price_feed_for(w)
                s_now = feed.current() if feed is not None else mc.s_now
                s_open = None
                try:
                    snap = feed.open_snapshot(getattr(w, "event_id", None)) if feed else None
                    if snap is not None:
                        s_open = getattr(snap, "price", None) or (
                            snap.get("price") if isinstance(snap, dict) else None)
                    s_open = s_open or mc.s_open
                except Exception:  # noqa: BLE001
                    s_open = mc.s_open
                ask = None
                if proposed_side == "up":
                    ask = (w.up_book.best_ask if w.up_book else None)
                elif proposed_side == "down":
                    ask = (w.down_book.best_ask if w.down_book else None)
                sigma_use = sigma
                if feed is not None and hasattr(feed, "sigma_per_sec"):
                    try:
                        sigma_use = feed.sigma_per_sec(now) or sigma
                    except Exception:  # noqa: BLE001
                        sigma_use = sigma
                bi = self.binary_intel.analyze_pre_trade(
                    intake=self.tradingview,
                    window=w,
                    s_now=s_now,
                    s_open=s_open,
                    sigma_per_sec=sigma_use,
                    ttc_s=float(ttc),
                    window_seconds=float(ws),
                    poly_mid=mc.poly_yes,
                    model_p_up=fair_used,
                    proposed_side=proposed_side,
                    ask=ask,
                    now=float(now),
                    readiness_score=analysis.get("score"),
                )
                if bi:
                    analysis["binary_intel"] = bi
                    analysis["binary_intel_score"] = bi.get("composite_score")
                    analysis["binary_intel_size_mult"] = bi.get("size_mult")
                    analysis["binary_intel_recommendation"] = bi.get("recommendation")
                    try:
                        dr.binary_intel = bi
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001 — never block on intel errors
                logger.exception("binary_intel pre-trade failed")
        return analysis

    def _grok_decision_bundle(self, mc, dr, w, fair_used, ttc, tv_feature) -> dict:
        """Fully-structured 'analyze everything' payload for the Grok decider. Numerics rounded,
        nulls allowed, ordered so the decision-critical fields lead. Includes: market microstructure
        + binary payoff (the breakeven bar), the digital fair vs Polymarket divergence, the
        TradingView signal, live news, regime/research, edge signal, account/risk state, the bot's
        OWN learned evidence, and the decider's track record (so Grok LEARNS as it trades)."""
        rf = dr.features or {}
        try:
            sel_be = self.selectivity_gate.bucket_evidence(self.selectivity_evidence, top=6)
        except Exception:  # noqa: BLE001
            sel_be = {}
        ls = self.ledger.stats()
        up_ask = (w.up_book.best_ask if w.up_book else None)
        dn_ask = (w.down_book.best_ask if w.down_book else None)
        poly_yes = mc.poly_yes
        divergence = (round(float(fair_used) - float(poly_yes), 4)
                      if (fair_used is not None and poly_yes is not None) else None)
        # compact TradingView signal: drop nulls/unknowns to keep the payload tight + readable
        tv = None
        if tv_feature:
            tv = {k: v for k, v in tv_feature.items()
                  if v is not None and v != "unknown" and k not in ("observe_only", "source")}
        series_label = getattr(w, "series_label", mc.series_label)
        _ws = int(getattr(w, "window_seconds", mc.window_seconds) or 300)
        mtf = None
        tv_trend = None
        tv_alert_history = None
        tv_15m_price_path = None
        tv_rsi_band = None
        tv_rsi_divergence = None
        tv_alert_interpretation = None
        tv_2h_review = None
        tv_per_tf_ladder = None
        price_action_trend = None
        tv_chart_lane = None
        _grok_trend_src = (self.cfg.grok_trend_source or "price").strip().lower()
        if _grok_trend_src == "price":
            price_action_trend = self._grok_price_action_trend(w, self.last_tick_ts or time.time())
        if self.tradingview is not None:
            from engine.pulse.tradingview import tv_lane_metadata_for_window, tv_symbol_for_window
            _tv_sym = tv_symbol_for_window(
                w, default_btc=self.cfg.tradingview_feature_symbol)
            tv_chart_lane = tv_lane_metadata_for_window(w)
            _lane_tfs = self._tv_mtf_timeframes_for_window(w)
            mtf = self.tradingview.mtf_confirmation(
                symbol=_tv_sym, now=self.last_tick_ts, tfs=_lane_tfs)
            tv_per_tf_ladder = self._tv_per_tf_views(
                self.last_tick_ts or time.time(), symbol=_tv_sym, tfs=_lane_tfs)
            tv_rep = self.tradingview.report()
            from engine.pulse.grok_bundle import tv_alert_history_snapshot, tv_trend_snapshot
            tv_trend = tv_trend_snapshot(
                mtf=mtf,
                latest_by_timeframe=tv_rep.get("tradingview_latest_by_timeframe") or {},
                feature_symbol=_tv_sym,
            )
            _hist_cap = max(1, int(self.cfg.tradingview_alert_history_per_symbol or 50))
            _short_n = max(6, min(int(getattr(self.cfg, "tv_15m_short_path_n", 8) or 8), _hist_cap))
            _hist_snap = self.tradingview.alert_history_snapshot(focus_symbol=_tv_sym)
            tv_alert_history = tv_alert_history_snapshot(
                history=_hist_snap,
                focus_symbol=_tv_sym,
                per_symbol_limit=_hist_cap,
            )
            from engine.pulse.tv_15m_price_path import tv_15m_price_path_snapshot
            tv_15m_price_path = tv_15m_price_path_snapshot(
                history=_hist_snap,
                focus_symbol=_tv_sym,
                max_points=_hist_cap,
                short_n=_short_n,
            )
            tv_rsi_band = self._tv_rsi_band_for_window(w, self.last_tick_ts or time.time())
            tv_rsi_divergence = self._tv_rsi_divergence_for_window(
                w, self.last_tick_ts or time.time())
            from engine.pulse.tv_alert_interpretation import interpret_tv_for_window
            tv_alert_interpretation = interpret_tv_for_window(
                window_seconds=_ws,
                series_label=series_label,
                tv_chart_lane=tv_chart_lane,
                price_path=tv_15m_price_path,
                rsi_band=tv_rsi_band,
                rsi_divergence=tv_rsi_divergence,
                alert_history=tv_alert_history,
                tv_trend=tv_trend,
            )
            if self.cfg.tv_2h_review_enabled:
                tv_2h_review = self._tv_2h_for_window(w, self.last_tick_ts)
        from engine.pulse.grok_bundle import (compact_tv_learning, gate_funnel_top,
                                              grok_task_for_window)
        from engine.pulse.reporting import ledger_stats_by_market_series
        lifecycle = self.reconciler.report()
        return {
            "schema_version": "grok_decision_bundle/1.4",
            "grok_task": grok_task_for_window(series_label=series_label, window_seconds=_ws,
                                               ttc_s=ttc),
            "market": "polymarket_btc_%s_up_or_down" % series_label,
            "series_slug": getattr(w, "series_slug", mc.series_slug),
            "series_label": series_label,
            "window_seconds": _ws,
            "objective": ("settles UP if BTC Chainlink close >= window open (%s window); "
                          "pick up/down/no_trade") % series_label,
            "decision_id": mc.decision_id,
            "by_market_series": ledger_stats_by_market_series(self.ledger.positions),
            "gate_funnel": gate_funnel_top(lifecycle.get("rejected_by_stage") or {}),
            "price_action_trend": price_action_trend,
            "tradingview_trend": tv_trend,
            "tradingview_per_tf_ladder": tv_per_tf_ladder,
            "tradingview_2h_review": tv_2h_review,
            "tradingview_alert_history": tv_alert_history,
            "tradingview_15m_price_path": tv_15m_price_path,
            "tradingview_rsi_band": tv_rsi_band,
            "tradingview_rsi_divergence": tv_rsi_divergence,
            "tradingview_alert_interpretation": tv_alert_interpretation,
            "tv_chart_lane": tv_lane_metadata_for_window(w),
            "tv_signal_learning": compact_tv_learning(self._tv_learner.report(
                promotion_allowed=self.cfg.tradingview_promotion_allowed,
                min_samples=self.cfg.tradingview_promotion_min_samples,
                min_win_rate=self.cfg.tradingview_promotion_min_win_rate)),
            "timing": {"seconds_to_close": self._r(ttc, 1),
                       "seconds_since_open": self._r(w.seconds_since_open(self.last_tick_ts or time.time()), 1),
                       "window_seconds": int(getattr(w, "window_seconds", mc.window_seconds) or 300),
                       "utc_minute_of_hour": int((self.last_tick_ts or time.time()) // 60 % 60)},
            "pre_trade_analysis": (dr.pre_trade if getattr(dr, "pre_trade", None) else None),
            "binary_intel": (getattr(dr, "binary_intel", None)
                             or ((dr.pre_trade or {}).get("binary_intel")
                                 if getattr(dr, "pre_trade", None) else None)
                             or (self.binary_intel._last_pre
                                 if getattr(self, "binary_intel", None) is not None else None)),
            "binary_intel_learner": (self.binary_intel.report()
                                     if getattr(self, "binary_intel", None) is not None else None),
            "price": {"btc_now": self._r(mc.s_now, 2), "btc_open": self._r(mc.s_open, 2),
                      "eth_now": (self._r(self._eth_price.current(), 2)
                                  if self._eth_price is not None else None),
                      "move_from_open": (self._r(mc.s_now - mc.s_open, 2)
                                         if (mc.s_now is not None and mc.s_open is not None) else None),
                      "sigma_per_sec": self._r(mc.sigma_per_sec, 6),
                      "lead_prices": {k: self._r(v, 2) for k, v in (mc.lead_prices or {}).items()
                                      if v is not None}},
            "digital_fair_p_up": self._r(fair_used),
            "polymarket": {
                "yes_mid": self._r(poly_yes), "spread": self._r(mc.spread),
                "up_best_ask": self._r(up_ask), "down_best_ask": self._r(dn_ask),
                "ask_depth_usd": self._r(mc.ask_depth_usd, 1),
                "fair_minus_poly": divergence,
                "up_book": self._book_side_snapshot(w.up_book),
                "down_book": self._book_side_snapshot(w.down_book),
            },
            "active_markets": self._active_markets_for_grok(),
            "cex_prices": self._cex_prices_snapshot(),
            "payoff": {"up": self._reward_risk(up_ask), "down": self._reward_risk(dn_ask),
                       "min_reward_risk_floor": self.cfg.min_reward_risk,
                       "note": "only trade a side if your P(win) clears its breakeven_win_rate after costs"},
            "recent_windows": self._recent_windows_view(10),
            "trade_decision_history": self.trade_history.view_for_grok(50),
            "lessons": self.lessons.recent(10),
            "tradingview_signal": tv,
            "news": (self.grok_news.latest() if self.grok_news is not None else None),
            "research": {"hurst_regime": rf.get("hurst_regime"),
                         "zscore_bucket": rf.get("zscore_bucket"),
                         "half_life_s": self._r(rf.get("half_life_s"), 1),
                         "regime": (dr.regime or {}).get("state")},
            "edge_signal": {k: (dr.edge or {}).get(k) for k in
                            ("pulse_edge_score", "stale_divergence_class", "cex_agreement_bucket",
                             "orderbook_pressure")},
            # PRIMARY mispricing signal: fresh CEX-implied P(up) vs the market price (lead-lag), with
            # orderflow + TradingView + late-window confirmation. This is the credible edge to exploit.
            "cex_lead_mispricing": {k: (dr.cex_lead or {}).get(k) for k in
                                    ("divergence", "side", "confirmed", "tv_confirms",
                                     "late_decisive", "news_state", "cex_p_up", "poly_yes")},
            # the bot's directional model is graded WORSE than the market price out-of-sample; trust
            # the market price + divergence-based mispricing over the model's raw opinion.
            "model_vs_market": self._market_benchmark(),
            "edge_model_p_up": self._r((dr.model or {}).get("p_up")),
            "grok_per_signal_p_up": (tv_feature or {}).get("grok_p_up"),
            "account_state": {"open_positions": ls.get("open_positions"),
                              "settled": ls.get("settled"), "win_rate": self._r(ls.get("win_rate")),
                              "realized_pnl_usd": self._r(ls.get("realized_pnl_usd"), 2),
                              "daily_loss_so_far_usd": self._r(self._daily_loss, 2),
                              "size_usd": self.cfg.size_usd},
            "bot_learned_evidence": {
                "selectivity_blocked_or_notable": sel_be.get("buckets", [])[:6],
                "late_window_edge_verdict": self.late_window_edge.report().get("verdict"),
                "pnl_by_ttc_bucket": self._groups.summary().get("ttc_bucket", {}),
                "pnl_by_hurst_regime": self._groups.summary().get("hurst_regime", {})},
            "decider_track_record": (self.grok_decider.report() if self.grok_decider else {}),
            "note": ("advisory PAPER decision; the bot enforces a realism/risk floor (execution gate, "
                     "caps, freshness) and follows your direction; learn from decider_track_record."),
        }

    @staticmethod
    def _counterfactual_side_pnl(side: str, entry_price: float, size_usd: float,
                                   outcome_up: bool):
        if side not in ("up", "down") or not entry_price or entry_price <= 0 or entry_price >= 1:
            return None, 0.0
        won = (side == "up" and outcome_up) or (side == "down" and not outcome_up)
        shares = float(size_usd) / float(entry_price)
        pnl = round((shares if won else 0.0) - float(size_usd), 6)
        return bool(won), pnl

    @staticmethod
    def _grok_proposed_side(grok_dec: Optional[dict]) -> Optional[str]:
        if not grok_dec:
            return None
        act = grok_dec.get("action")
        return act if act in ("up", "down") else None

    def _schedule_verifier_grade(self, decision_id: str, *, price0, close_ts: float, side: str,
                                 entry_ask: float, size_usd: float, acted: bool) -> None:
        """Queue a verifier verdict for counterfactual grading at window close."""
        if (not decision_id or side not in ("up", "down") or price0 is None
                or entry_ask is None or self.verifier is None):
            return
        for p in self._verifier_pending:
            if p["decision_id"] == decision_id:
                return
        self._verifier_pending.append({
            "decision_id": decision_id,
            "price0": float(price0),
            "close_ts": float(close_ts),
            "side": side,
            "entry_ask": float(entry_ask),
            "size_usd": float(size_usd),
            "acted": bool(acted),
        })

    def _maybe_schedule_verifier_counterfactual(self, mc, w, snap, grok_dec, *,
                                                side=None, entry_ask=None,
                                                size_frac: float = 1.0, acted: bool = False) -> None:
        """Schedule counterfactual P&L grade when Claude has a final verdict on a proposed side."""
        if self.verifier is None or acted:
            return
        verdict = self.verifier.get(mc.decision_id)
        if not verdict or verdict.get("pending") or verdict.get("approve"):
            return
        side = side or self._grok_proposed_side(grok_dec)
        if side not in ("up", "down"):
            return
        if entry_ask is None:
            book = w.up_book if side == "up" else w.down_book
            entry_ask = book.best_ask if book else None
        if entry_ask is None or snap.price is None:
            return
        size_usd = float(self.cfg.size_usd) * float(size_frac or 1.0)
        self._schedule_verifier_grade(
            mc.decision_id, price0=snap.price, close_ts=w.close_ts, side=side,
            entry_ask=float(entry_ask), size_usd=size_usd, acted=False)

    def _grade_verifier_decisions(self, now: float) -> None:
        """Grade due verifier verdicts vs the realized 5-min outcome (veto counterfactual P&L)."""
        if not self._verifier_pending or self.verifier is None:
            return
        px = self.price.current()
        still = []
        for p in self._verifier_pending:
            if now < p["close_ts"]:
                still.append(p)
                continue
            if px is not None:
                outcome_up = float(px) >= float(p["price0"])
                won, pnl = self._counterfactual_side_pnl(
                    p["side"], p["entry_ask"], p["size_usd"], outcome_up)
                if won is not None:
                    self.verifier.grade(p["decision_id"], won=won, pnl=pnl,
                                        acted=bool(p.get("acted")))
            elif now <= p["close_ts"] + 600:
                still.append(p)
        self._verifier_pending = still[-2000:]

    def _schedule_grok_grade(self, decision_id: str, price0, close_ts: float, decision: dict) -> None:
        """Queue a decision for grading at window close. The gradeable fields (action/p_up/context)
        are SNAPSHOTTED here and persisted, so grading survives a process restart (the decider's
        in-memory result cache does not)."""
        if price0 is None:
            return
        for p in self._grok_pending:
            if p["decision_id"] == decision_id:
                return
        self._grok_pending.append({"decision_id": decision_id, "price0": float(price0),
                                   "close_ts": float(close_ts),
                                   "action": decision.get("action"), "p_up": decision.get("p_up"),
                                   "context": decision.get("context") or {}})

    def _grade_grok_decisions(self, now: float) -> None:
        """Grade due Grok decisions vs the realized 5-min outcome (UP if close >= open), traded or
        not. Leakage-free (price0 snapshotted at entry). Uses the persisted snapshot so it survives
        restarts. This is the always-on directional edge data Grok learns from."""
        if not self._grok_pending or self.grok_decider is None:
            return
        px = self.price.current()
        still = []
        for p in self._grok_pending:
            if now < p["close_ts"]:
                still.append(p)
                continue
            if px is not None:
                s_open, s_close = float(p["price0"]), float(px)
                outcome_up = s_close >= s_open
                self.grok_decider.grade_fields(
                    action=p.get("action"), p_up=p.get("p_up"), context=p.get("context") or {},
                    outcome_up=outcome_up)
                # record the resolved window so Grok sees the recent sequence of outcomes
                self._recent_windows.append({
                    "close_ts": round(float(p["close_ts"]), 1), "s_open": round(s_open, 2),
                    "s_close": round(s_close, 2), "outcome": ("up" if outcome_up else "down"),
                    "move_pct": (round((s_close - s_open) / s_open * 100, 4) if s_open else None)})
                self._recent_windows = self._recent_windows[-40:]
            elif now <= p["close_ts"] + 600:
                still.append(p)
        self._grok_pending = still[-2000:]

    def _schedule_council_grade(self, decision_id: str, price0, close_ts: float,
                                views: dict) -> None:
        """Snapshot the LLM-council member views for grading at window close (restart-safe)."""
        if price0 is None or self.llm_council is None:
            return
        for p in self._council_pending:
            if p["decision_id"] == decision_id:
                return
        self._council_pending.append({"decision_id": decision_id, "price0": float(price0),
                                      "close_ts": float(close_ts), "views": dict(views or {})})

    def _grade_council_decisions(self, now: float) -> None:
        """Grade each council member's p_up view vs the realized close (UP if close >= open). This is
        how the council learns which member to trust and re-weights them. Leakage-free + restart-safe."""
        if not self._council_pending or self.llm_council is None:
            return
        px = self.price.current()
        still = []
        for p in self._council_pending:
            if now < p["close_ts"]:
                still.append(p)
                continue
            if px is not None:
                outcome_up = float(px) >= float(p["price0"])
                self.llm_council.grade(p.get("views") or {}, outcome_up)
            elif now <= p["close_ts"] + 600:
                still.append(p)
        self._council_pending = still[-2000:]

    def _tv_per_tf_views(self, now: float, *, symbol: Optional[str] = None,
                         tfs: Optional[tuple] = None) -> dict:
        """Per-timeframe TV directional views for the council: ``{tv_<tf>m: p_up}``, fresh alerts
        only, freshness scaled by TF (a 15m trend stays informative longer than a 2m burst). Each TF
        is graded independently by the council so its stance reveals which timeframe earns a FOLLOW."""
        out: dict = {}
        tvi = self.tradingview
        if tvi is None:
            return out
        active_tfs = frozenset(str(t) for t in (tfs or self.cfg.tradingview_mtf_timeframes or ()))
        try:
            from engine.pulse.tradingview import canonical_storage_symbol
            now = float(now)
            want_sym = (canonical_storage_symbol(symbol, self.cfg.tradingview_feature_symbol)
                        if symbol else None)
            for (_sym, tf), pair in list(getattr(tvi, "latest_by_tf", {}) .items()):
                try:
                    ev, ts = pair
                    tf_key = str(tf)
                    if active_tfs and tf_key not in active_tfs:
                        continue
                    if want_sym and canonical_storage_symbol(_sym, self.cfg.tradingview_feature_symbol) != want_sym:
                        continue
                    tfn = int(tf_key)
                except (TypeError, ValueError):
                    continue
                # freshness: lenient per-TF window, but HARD-capped to the window clock so a stale,
                # cadence-misaligned read (e.g. a 1h alert at :45, ~45 min old) can't vote on a bet
                # placed at the 15m window open. Off-grid TFs simply drop out when they go stale.
                if (now - float(ts)) > min(max(300.0, tfn * 60.0 * 2.5),
                                           float(self.cfg.council_tv_max_age_s)):
                    continue
                d = str(getattr(ev, "direction", "") or "").upper()
                s = getattr(ev, "strength", None)
                if s is None or d not in ("UP", "DOWN"):
                    continue
                s = max(0.0, min(1.0, float(s)))
                out["tv_%dm" % tfn] = (0.5 + 0.5 * s) if d == "UP" else (0.5 - 0.5 * s)
        except Exception:  # noqa: BLE001 — never break the tick over TV parsing
            return out
        return out

    def _tv_2h_trend_view(self, now: float, *, symbol: Optional[str] = None) -> Optional[float]:
        """2h TV trend p_up view for council grading only (not a voter unless promoted later)."""
        if not self.cfg.tv_2h_review_enabled or not self.cfg.tv_2h_council_grade:
            return None
        from engine.pulse.tv_2h_review import tv_2h_trend_p_up
        sym = symbol or self.cfg.tradingview_feature_symbol
        review = self._tv_2h_review_for_symbol(sym, now)
        return tv_2h_trend_p_up(review)

    def _tv_mtf_view(self, now: float, *, symbol: Optional[str] = None,
                     tfs: Optional[tuple] = None) -> Optional[float]:
        """Single multi-timeframe AGREEMENT view (p_up in [0,1]) combining fresh per-TF TV alerts.
        Slower intrahour TFs (45m, 55m) weigh more than 15m/30m so the ladder reflects price trend."""
        try:
            ladder_tfs = tuple(tfs or self.cfg.tradingview_mtf_timeframes or ())
            per_tf = self._tv_per_tf_views(now, symbol=symbol, tfs=ladder_tfs)
            weighted: list[tuple[float, float]] = []
            for i, tf in enumerate(ladder_tfs):
                key = "tv_%sm" % tf
                if key not in per_tf:
                    continue
                w = float(i + 1)
                weighted.append((float(per_tf[key]) - 0.5, w))
            if not weighted:
                return None
            total_w = sum(w for _, w in weighted)
            avg = sum(lean * w for lean, w in weighted) / total_w
            leans = [lean for lean, _ in weighted]
            disp = sum(abs(x) for x in leans) / len(leans)
            agree = (abs(avg) / disp) if disp > 0 else 0.0
            comp = avg * agree
            return max(0.0, min(1.0, 0.5 + comp))
        except Exception:  # noqa: BLE001 — never break the tick over TV parsing
            return None

    def _schedule_cex_lead_grade(self, decision_id: str, price0, close_ts: float,
                                 sig: dict) -> None:
        """Queue a CEX-lead signal for grading at window close. The gradeable fields are SNAPSHOTTED
        and persisted, so grading survives a restart (leakage-free: price0 captured at entry)."""
        if price0 is None or self.cex_lead is None or not sig.get("has_signal"):
            return
        for p in self._cex_lead_pending:
            if p["decision_id"] == decision_id:
                return
        self._cex_lead_pending.append({
            "decision_id": decision_id, "price0": float(price0), "close_ts": float(close_ts),
            "bucket": sig.get("bucket"), "context_keys": sig.get("context_keys") or [],
            "side": sig.get("side"), "cex_p_up": sig.get("cex_p_up"),
            "poly_yes": sig.get("poly_yes"), "fair": sig.get("fair")})

    def _grade_cex_lead(self, now: float) -> None:
        """Grade due CEX-lead signals vs the realized 5-min outcome (UP if close >= open), traded or
        not. This is the always-on, unbiased measurement of whether the CEX-implied probability beats
        the market price — the gate for ever promoting it to drive trades."""
        if not self._cex_lead_pending or self.cex_lead is None:
            return
        px = self.price.current()
        still = []
        for p in self._cex_lead_pending:
            if now < p["close_ts"]:
                still.append(p)
                continue
            if px is not None:
                outcome_up = float(px) >= float(p["price0"])
                if p.get("cex_p_up") is not None and p.get("poly_yes") is not None:
                    self.cex_lead.record(
                        bucket=p.get("bucket"), context_keys=p.get("context_keys"),
                        side=p.get("side"), cex_p_up=p["cex_p_up"], poly_yes=p["poly_yes"],
                        fair=p.get("fair"), outcome_up=outcome_up)
            elif now <= p["close_ts"] + 600:
                still.append(p)
        self._cex_lead_pending = still[-2000:]

    def _schedule_market_benchmark(self, decision_id: str, price0, close_ts: float,
                                   model_p_up, market_p_up, fair_p_up) -> None:
        """Queue a model-vs-market accuracy grade at window close (leakage-free snapshot)."""
        if price0 is None or model_p_up is None or market_p_up is None:
            return
        for p in self._mkt_bench_pending:
            if p["decision_id"] == decision_id:
                return
        self._mkt_bench_pending.append({
            "decision_id": decision_id, "price0": float(price0), "close_ts": float(close_ts),
            "model_p_up": float(model_p_up), "market_p_up": float(market_p_up),
            "fair_p_up": (float(fair_p_up) if fair_p_up is not None else None)})

    def _grade_market_benchmark(self, now: float) -> None:
        """Grade due windows: accumulate squared error of model P(up), market price, and digital fair
        vs the realized outcome — the rolling comparison powering the learning blend's market gate."""
        if not self._mkt_bench_pending:
            return
        px = self.price.current()
        still = []
        for p in self._mkt_bench_pending:
            if now < p["close_ts"]:
                still.append(p)
                continue
            if px is not None:
                o = 1.0 if float(px) >= float(p["price0"]) else 0.0
                m_se = (float(p["model_p_up"]) - o) ** 2
                k_se = (float(p["market_p_up"]) - o) ** 2
                f_se = ((float(p["fair_p_up"]) - o) ** 2 if p.get("fair_p_up") is not None else None)
                self._mkt_bench_recent.append((m_se, k_se, f_se))
            elif now <= p["close_ts"] + 600:
                still.append(p)
        self._mkt_bench_pending = still[-2000:]

    def _market_benchmark(self) -> dict:
        """Rolling Brier of model vs market vs digital-fair on graded windows (out-of-sample)."""
        rows = list(self._mkt_bench_recent)
        n = len(rows)
        if n == 0:
            return {"n": 0, "model_brier": None, "market_brier": None, "fair_brier": None,
                    "model_beats_market": None}
        mb = sum(r[0] for r in rows) / n
        kb = sum(r[1] for r in rows) / n
        fr = [r[2] for r in rows if r[2] is not None]
        fb = (sum(fr) / len(fr)) if fr else None
        return {"n": n, "model_brier": round(mb, 5), "market_brier": round(kb, 5),
                "fair_brier": (round(fb, 5) if fb is not None else None),
                "model_beats_market": bool(mb < kb)}

    def _mc_scenario_context(self) -> dict:
        """Evidence for the LLM MC scenario advisor so it can actually shade the model (vol regime,
        momentum, regime) rather than seeing a thin window list and always returning neutral. All
        fields best-effort; the LLM's params are still clamped to safe bounds + graded (calibration)."""
        ctx = {"recent_windows": self._recent_windows_view(6),
               "neutral_realized_vol_per_sec": 7e-5,          # ~typical live level, so it can judge regime
               "note": "sigma_mult>1 if realized_vol is elevated vs neutral; mu tiny; jumps only on event risk"}
        try:
            now = float(self.last_tick_ts or 0.0)
            sig = self.price.sigma_per_sec(now)
            ctx["realized_vol_per_sec"] = round(float(sig), 8) if sig else None
            cur = self.price.current()
            ctx["btc_price"] = round(float(cur), 2) if cur is not None else None
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.markov is not None:
                rep = self.markov.report() or {}
                ctx["markov_regime"] = rep.get("current_state") or rep.get("state") or rep.get("regime")
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.grok_news is not None:
                nd = self.grok_news.report() or {}
                ctx["news"] = {k: nd.get(k) for k in ("sentiment", "event_risk", "risk", "headline")
                               if nd.get(k) is not None}
        except Exception:  # noqa: BLE001
            pass
        # CEX leads + TV path for Grok-MC parameterization
        try:
            latest = getattr(self.leads, "_latest", {}) or {}
            bn = (latest.get("binance_btcusdt") or (None,))[0]
            cb = (latest.get("coinbase_btcusd") or (None,))[0]
            ctx["cex"] = {
                "binance": round(float(bn), 2) if bn else None,
                "coinbase": round(float(cb), 2) if cb else None,
            }
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.tradingview is not None:
                from engine.pulse.tradingview import (
                    tv_chart_symbol_for_window,
                    tv_lane_metadata_for_window,
                )
                from engine.pulse.tv_15m_price_path import (
                    compact_path_for_plot, dual_horizon_price_path,
                    resolve_bar_close_from_intake, trade_lean_from_path)
                from engine.pulse.tv_rsi_overlay import resolve_rsi_overlay_from_intake
                tv_sym = self.cfg.tradingview_feature_symbol
                tv_lane = None
                mc_ws = 900
                mc_label = ""
                try:
                    now_mc = float(self.last_tick_ts or time.time())
                    for w in self._directional_windows(now_mc, require_open=True):
                        tv_sym = tv_chart_symbol_for_window(w) or tv_sym
                        tv_lane = tv_lane_metadata_for_window(w)
                        mc_ws = int(getattr(w, "window_seconds", 900) or 900)
                        mc_label = str(getattr(w, "series_label", "") or "")
                        break
                except Exception:  # noqa: BLE001
                    pass
                sym, hist = resolve_bar_close_from_intake(self.tradingview, tv_sym)
                dual = dual_horizon_price_path(hist, regime_n=50, short_n=8)
                lean = trade_lean_from_path(dual)
                plot = compact_path_for_plot(dual)
                ctx["tv_5m_price_pattern"] = {
                    "symbol": sym,
                    "trade_lean": lean.get("trade_lean"),
                    "alignment": lean.get("alignment"),
                    "confidence": lean.get("confidence"),
                    "short_pattern": lean.get("short_pattern"),
                    "regime_pattern": lean.get("regime_pattern"),
                    "short_path": plot.get("short_path"),
                    "regime_path_tail": plot.get("regime_path_tail"),
                    "note": plot.get("note"),
                }
                if tv_lane:
                    ctx["tv_chart_lane"] = tv_lane
                now_tv = float(self.last_tick_ts or time.time())
                ov = resolve_rsi_overlay_from_intake(
                    self.tradingview, tv_sym, now=now_tv)
                if ov:
                    ctx["tv_rsi_overlay"] = {
                        "lean": ov.get("lean"), "age_s": ov.get("age_s"),
                        "rsi": ov.get("rsi"), "signal_level": ov.get("signal_level"),
                        "symbol": ov.get("resolved_symbol") or ov.get("symbol"),
                    }
                from engine.pulse.tv_rsi_divergence import resolve_rsi_divergence_from_intake
                div = resolve_rsi_divergence_from_intake(
                    self.tradingview, tv_sym,
                    now=now_tv,
                    max_age_s=float(getattr(self.cfg, "tv_rsi_overlay_max_age_s", 2700.0) or 2700.0))
                if div:
                    ctx["tv_rsi_divergence"] = {
                        "has_signal": div.get("has_signal"),
                        "primer": div.get("primer"),
                        "latest": div.get("latest"),
                        "history_summary": div.get("history_summary"),
                        "confirm_fade_by_side": div.get("confirm_fade_by_side"),
                        "symbol": div.get("resolved_symbol"),
                    }
                from engine.pulse.tv_rsi_band import resolve_rsi_band_from_intake
                band = resolve_rsi_band_from_intake(
                    self.tradingview, tv_sym,
                    now=now_tv,
                    max_age_s=float(getattr(self.cfg, "tv_rsi_band_max_age_s", 900.0) or 900.0))
                if band:
                    ctx["tv_rsi_band"] = {
                        "rsi": band.get("rsi"),
                        "rsi_zone": band.get("rsi_zone"),
                        "lean": band.get("lean"),
                        "band_event": band.get("band_event"),
                        "age_s": band.get("age_s"),
                        "oversold_threshold": band.get("oversold_threshold"),
                        "overbought_threshold": band.get("overbought_threshold"),
                        "history_summary": band.get("history_summary"),
                        "symbol": band.get("resolved_symbol") or band.get("symbol"),
                    }
                from engine.pulse.tv_alert_interpretation import interpret_tv_for_window
                ctx["tv_alert_interpretation"] = interpret_tv_for_window(
                    window_seconds=mc_ws,
                    series_label=mc_label,
                    tv_chart_lane=tv_lane,
                    price_path=ctx.get("tv_5m_price_pattern"),
                    rsi_band=ctx.get("tv_rsi_band"),
                    rsi_divergence=ctx.get("tv_rsi_divergence"),
                )
        except Exception:  # noqa: BLE001
            pass
        try:
            if getattr(self, "p_exec_tune", None) is not None:
                ctx["p_exec_self_tune"] = {
                    "w_mc": self.p_exec_tune.w_mc,
                    "promoted_n": len(self.p_exec_tune.promoted),
                }
        except Exception:  # noqa: BLE001
            pass
        return ctx

    def _run_directional_mc(self, *, s_now: float, s_open: float, sigma: float,
                            ttc: float) -> dict:
        """Grok-param MC for directional P(close >= open). Fail-open to empty."""
        empty = {"available": False}
        if not bool(getattr(self.cfg, "dir_mc_enabled", True)):
            return empty
        try:
            from engine.pulse.monte_carlo import HAVE_NUMPY, mc_directional_p_up, NEUTRAL_SCENARIO
            if not HAVE_NUMPY:
                return empty
            params = dict(NEUTRAL_SCENARIO)
            if self.mc_scenario is not None:
                params = self.mc_scenario.latest() or params
            return mc_directional_p_up(
                float(s_now), float(s_open), float(sigma), float(ttc),
                mu_per_sec=float(params.get("mu_per_sec") or 0.0),
                sigma_mult=float(params.get("sigma_mult") or 1.0),
                jump_intensity_per_sec=float(params.get("jump_intensity_per_sec") or 0.0),
                jump_sigma=float(params.get("jump_sigma") or 0.0),
                n_paths=int(getattr(self.cfg, "dir_mc_paths", 8000) or 8000),
                crash_threshold_pct=float(params.get("crash_threshold_pct") or 1.5),
                control_alpha=float(getattr(self.cfg, "dir_mc_control_alpha", 0.5) or 0.5),
            )
        except Exception:  # noqa: BLE001
            return empty

    def _build_p_exec(self, *, side: str, fair_used: Optional[float], poly_yes: Optional[float],
                      vwap: Optional[float], s_now: float, s_open: float, sigma: float,
                      ttc: float, sso: float, asset: str, horizon: str,
                      lead_state: str = "none") -> dict:
        """Unified directional probability: MC + digital + mkt → p_exec(c)."""
        from engine.pulse.p_exec import (
            blend_p, compute_p_exec, context_key)
        side_l = str(side or "").lower()
        p_dig = float(fair_used) if fair_used is not None else None
        p_mkt = None
        if poly_yes is not None:
            py = float(poly_yes)
            p_mkt = py if side_l == "up" else (1.0 - py)
        mc = self._run_directional_mc(s_now=s_now, s_open=s_open, sigma=sigma, ttc=ttc)
        p_mc_up = mc.get("p_mc_adj") if mc.get("available") else None
        p_mc = None
        if p_mc_up is not None:
            p_mc = float(p_mc_up) if side_l == "up" else (1.0 - float(p_mc_up))
        weights = (self.p_exec_tune.blend_weights()
                   if getattr(self, "p_exec_tune", None) is not None
                   else {"w_mkt": 0.5, "w_dig": 0.5, "w_mc": 0.0})
        if side_l == "down" and p_dig is not None:
            p_dig_side = 1.0 - p_dig
        else:
            p_dig_side = p_dig
        p_blend = blend_p(p_mkt=p_mkt, p_digital=p_dig_side, p_mc=p_mc, **weights)
        ck = context_key(asset=asset, horizon=horizon, side=side_l, ttc_s=ttc,
                         vwap=float(vwap or 0), sso_s=sso, lead_state=lead_state)
        wr = self.p_exec_tune.wr_emp(ck) if self.p_exec_tune is not None else None
        n_c = self.p_exec_tune.n_c(ck) if self.p_exec_tune is not None else 0
        p_exec = compute_p_exec(p_blend=p_blend, wr_emp=wr, n_c=n_c)
        allow, allow_reason = (True, "p_exec_disabled")
        if bool(getattr(self.cfg, "p_exec_enabled", True)) and self.p_exec_tune is not None:
            if bool(getattr(self.cfg, "p_exec_gate_cold", True)):
                allow, allow_reason = self.p_exec_tune.allow_trade(ck)
            else:
                allow, allow_reason = True, "gate_cold_off"
        crash_cap = float(getattr(self.cfg, "dir_mc_crash_cap", 0.25) or 0.25)
        p_crash = mc.get("p_crash")
        crash_block = (p_crash is not None and float(p_crash) >= crash_cap)
        min_vwap = float(getattr(self.cfg, "p_exec_min_vwap", 0.50) or 0.50)
        vwap_block = (vwap is not None and float(vwap) < min_vwap)
        return {
            "p_exec": p_exec,
            "p_blend": p_blend,
            "p_mkt": p_mkt,
            "p_digital_side": p_dig_side,
            "p_mc": p_mc,
            "mc": mc,
            "context_key": ck,
            "allow": allow and not crash_block and not vwap_block,
            "allow_reason": ("crash_cap" if crash_block
                             else ("vwap_floor" if vwap_block else allow_reason)),
            "weights": weights,
            "n_c": n_c,
            "wr_emp": wr,
        }

    def _recent_windows_view(self, n: int = 10) -> dict:
        """Recent resolved BTC 5m windows + a momentum summary (up-rate + current streak) for Grok."""
        rows = self._recent_windows[-n:]
        full = self._recent_windows[-20:]
        ups = sum(1 for w in full if w.get("outcome") == "up")
        streak = 0
        last = None
        for w in reversed(full):
            o = w.get("outcome")
            if last is None:
                last, streak = o, 1
            elif o == last:
                streak += 1
            else:
                break
        return {"windows": rows,
                "n": len(full),
                "up_rate": (round(ups / len(full), 3) if full else None),
                "current_streak": ((last or "") + "x" + str(streak)) if last else None}

    def _reward_risk_floor(self, side: "str | None") -> float:
        base = float(self.cfg.min_reward_risk or 0.0)
        if (base <= 0.0 or not side or str(side).lower() != "up"
                or not self.cfg.directional_up_restrictions_enabled):
            return base
        return base + float(self.cfg.min_reward_risk_up_premium or 0.15)

    def _ask_reward_risk_ok(self, side: "str | None", ask: "float | None") -> bool:
        floor = self._reward_risk_floor(side)
        if floor <= 0.0 or ask is None or float(ask) <= 0.0:
            return True
        return ((1.0 - float(ask)) / float(ask)) >= floor

    def _grok_up_side_allowed(self) -> bool:
        if self.grok_decider is None:
            return True
        rep = self.grok_decider.report()
        graded = int(rep.get("graded_directional") or 0)
        acc = rep.get("direction_accuracy")
        if graded >= 20 and acc is not None and float(acc) < 0.52:
            return False
        return True

    def _green_path_active(self, *, side: str, window_seconds: int) -> bool:
        """15m DOWN baseline quant: cohort + MTF; skip stacked opinion gates."""
        if not self.cfg.green_path_enabled:
            return False
        if side != "down":
            return False
        ws = int(window_seconds or 300)
        if ws < 900:
            return False
        return bool(self.cfg.baseline_cohort_15m_fast_lane)

    def _baseline_quant_cohort_ok(self, *, side: str, esnap=None, ttc_s: "float | None",
                                  tv_feature: "dict | None",
                                  window_seconds: int = 300,
                                  ask_price: "float | None" = None) -> "tuple[bool, str]":
        """Tier-1: baseline trades only in high-edge + strong-CEX + scaled TTC band; UP needs TV."""
        if not self.cfg.baseline_cohort_gate_enabled:
            return True, ""
        if side not in ("up", "down"):
            return False, "baseline_cohort_bad_side"
        if ttc_s is None:
            return False, "baseline_cohort_ttc_unknown"
        ws = int(window_seconds or 300)
        scale = float(ws) / 300.0
        fast_lane = (self.cfg.baseline_cohort_15m_fast_lane and ws >= 900)
        if fast_lane:
            ttc_min = float(self.cfg.baseline_cohort_15m_ttc_min_s) * scale
            ttc_max = float(self.cfg.baseline_cohort_15m_ttc_max_s) * scale
        else:
            ttc_min = float(self.cfg.baseline_cohort_ttc_min_s) * scale
            ttc_max = float(self.cfg.baseline_cohort_ttc_max_s) * scale
        ttc_f = float(ttc_s)
        if ttc_f > ttc_max:
            return False, "baseline_cohort_ttc_too_late"
        if ttc_f < ttc_min:
            return False, "baseline_cohort_ttc_too_early"
        edge_bucket = self._edge_snap_field(esnap, "pulse_edge_score_bucket")
        down_edge_relaxed = (
            fast_lane and side == "down"
            and not self.cfg.baseline_cohort_require_high_edge)
        if down_edge_relaxed:
            if edge_bucket not in ("medium", "high", "very_high"):
                return False, "baseline_cohort_edge_not_high"
        elif self.cfg.baseline_cohort_require_high_edge:
            if edge_bucket not in ("high", "very_high"):
                return False, "baseline_cohort_edge_not_high"
        cex_bucket = self._edge_snap_field(esnap, "cex_agreement_bucket")
        down_cex_relaxed = (
            fast_lane and side == "down"
            and not self.cfg.baseline_cohort_require_strong_cex)
        if down_cex_relaxed:
            if cex_bucket not in ("moderate", "strong"):
                return False, "baseline_cohort_cex_not_strong"
        elif self.cfg.baseline_cohort_require_strong_cex:
            if cex_bucket != "strong":
                return False, "baseline_cohort_cex_not_strong"
        if side == "up" and self.cfg.directional_up_restrictions_enabled:
            tv_ok, tv_reason = self._baseline_up_tv_strength_ok(tv_feature)
            if not tv_ok:
                return False, tv_reason
        if side == "down":
            if self.cfg.baseline_down_block_medium_edge:
                edge_bucket = self._edge_snap_field(esnap, "pulse_edge_score_bucket")
                if str(edge_bucket or "").strip().lower() == "medium":
                    return False, "baseline_down_medium_edge"
            if self.cfg.baseline_down_block_not_stale:
                stale = self._edge_snap_field(esnap, "stale_divergence_class")
                if str(stale or "").strip().lower() == "not_stale":
                    return False, "baseline_down_not_stale"
            if self.cfg.baseline_down_block_mid_entry and ask_price is not None:
                try:
                    ap = float(ask_price)
                except (TypeError, ValueError):
                    ap = None
                if ap is not None:
                    lo = float(self.cfg.baseline_down_mid_entry_min)
                    hi = float(self.cfg.baseline_down_mid_entry_max)
                    if lo <= ap < hi:
                        return False, "baseline_down_mid_entry_band"
            down_tv_ok, down_tv_reason = self._baseline_down_tv_context_ok(tv_feature)
            if not down_tv_ok:
                return False, down_tv_reason
        return True, ""

    def _config_coupling_report(self) -> dict:
        rep = dict(getattr(self, "_config_coupling", None) or {})
        if rep and self.tv_context_gate is not None:
            rep = {**rep, "runtime_context_max_ttc_s": self.tv_context_gate.max_ttc_s}
        return rep

    def _baseline_cohort_gate_report(self) -> dict:
        return {
            "enabled": bool(self.cfg.baseline_cohort_gate_enabled),
            "ttc_min_s": self.cfg.baseline_cohort_ttc_min_s,
            "ttc_max_s": self.cfg.baseline_cohort_ttc_max_s,
            "require_high_edge": self.cfg.baseline_cohort_require_high_edge,
            "require_strong_cex": self.cfg.baseline_cohort_require_strong_cex,
            "blocked": sum(self._baseline_cohort_gate_counts.values()),
            "block_reasons": dict(self._baseline_cohort_gate_counts),
            "15m_fast_lane": bool(self.cfg.baseline_cohort_15m_fast_lane),
            "15m_ttc_band_s": [self.cfg.baseline_cohort_15m_ttc_min_s,
                               self.cfg.baseline_cohort_15m_ttc_max_s],
            "up_restrictions_enabled": bool(self.cfg.directional_up_restrictions_enabled),
            "down_tv_gate_enabled": bool(self.cfg.baseline_down_tv_gate_enabled),
            "down_block_bullish_range": bool(self.cfg.baseline_down_block_bullish_range),
            "down_block_volume_active": bool(self.cfg.baseline_down_block_volume_active),
            "down_block_up_strong_range_top": bool(
                self.cfg.baseline_down_block_up_strong_range_top),
            "down_block_bullish_mtf": bool(self.cfg.baseline_down_block_bullish_mtf),
            "down_block_not_stale": bool(self.cfg.baseline_down_block_not_stale),
            "down_block_mid_entry": bool(self.cfg.baseline_down_block_mid_entry),
            "down_block_single_tf": bool(self.cfg.baseline_down_block_single_tf),
            "down_block_medium_edge": bool(self.cfg.baseline_down_block_medium_edge),
            "down_block_bb_expansion_down": bool(self.cfg.baseline_down_block_bb_expansion_down),
            "down_mid_entry_band": [self.cfg.baseline_down_mid_entry_min,
                                    self.cfg.baseline_down_mid_entry_max],
            "green_path_enabled": bool(self.cfg.green_path_enabled),
            "note": ("baseline quant path: 180-240s TTC band (scaled on 15m), high edge + "
                     "strong CEX; UP blocked until promoted; "
                     "green_path=15m DOWN cohort only (TV observe-only)"),
        }

    def _entry_confidence_tier(self, dr) -> "str | None":
        model = dr.model or {}
        if model.get("trained"):
            return _confidence_tier(model.get("model_confidence"))
        return _confidence_tier((dr.signals or {}).get("confidence"))

    def _down_bias_eval(self, *, side: str, tv_feature: "dict | None",
                        markov_state: "str | None" = None,
                        ttc_s: "float | None" = None,
                        esnap=None,
                        fair_p_up: "float | None" = None,
                        zscore_bucket: "str | None" = None,
                        confidence_tier: "str | None" = None,
                        ask_price: "float | None" = None) -> dict:
        from engine.pulse.late_window import conviction as _conviction
        feat = tv_feature or {}
        return self.tv_down_bias_gate.evaluate(
            side=side,
            mtf_alignment=feat.get("mtf_alignment"),
            tv_direction=feat.get("direction"),
            tf_confirm=feat.get("tf_confirm"),
            supertrend_direction=feat.get("supertrend_direction"),
            vwap_state=feat.get("vwap_state"),
            bb_state=feat.get("bb_state"),
            range_state=feat.get("range_state"),
            markov_state=markov_state,
            htf_bias=feat.get("htf_bias"),
            candle_pressure=feat.get("candle_pressure"),
            edge_score_bucket=self._edge_snap_field(esnap, "pulse_edge_score_bucket"),
            cex_agreement_bucket=self._edge_snap_field(esnap, "cex_agreement_bucket"),
            ob_pressure_bucket=self._edge_snap_ob_pressure(esnap),
            cvd_state=feat.get("cvd_state"),
            conviction=_conviction(fair_p_up),
            ttc_s=ttc_s,
            zscore_bucket=zscore_bucket,
            confidence_tier=confidence_tier,
            stale_divergence=self._edge_snap_field(esnap, "stale_divergence_class"),
            volume_state=feat.get("volume_state"),
            ask_price=ask_price,
        )

    def _up_side_tv_bias_ok(self, tv_feature: "dict | None",
                            ttc_s: "float | None" = None,
                            markov_state: "str | None" = None,
                            esnap=None,
                            fair_p_up: "float | None" = None,
                            dr=None,
                            rfeat=None,
                            ask_price: "float | None" = None) -> "tuple[bool, str]":
        """UP restrict-only: TV UP_STRONG plus down_bias pass (all entry modes)."""
        tv_ok, tv_reason = self._baseline_up_tv_strength_ok(tv_feature)
        if not tv_ok:
            return False, tv_reason
        db_res = self._down_bias_eval(side="up", tv_feature=tv_feature,
                                      markov_state=markov_state, ttc_s=ttc_s, esnap=esnap,
                                      fair_p_up=fair_p_up,
                                      zscore_bucket=(rfeat.zscore_bucket if rfeat else None),
                                      confidence_tier=(self._entry_confidence_tier(dr)
                                                       if dr is not None else None),
                                      ask_price=ask_price)
        if db_res["decision"] in ("block", "explore"):
            return False, db_res["reasons"][0]
        return True, ""

    def _asset_tv_feature(self, now: float, series_slug: str,
                          *, window=None) -> "dict | None":
        """Fresh per-asset TV feature for the window's series (1h multi-crypto)."""
        if self.tradingview is None:
            return None
        from engine.pulse.tradingview import tv_symbol_for_window
        sym = tv_symbol_for_window(window, series_slug=series_slug)
        if not sym:
            return None
        feat = self.tradingview.latest_feature_for_symbol(sym, now=now)
        if feat is None:
            return None
        age = feat.get("age_s")
        if age is not None and float(age) > float(self.cfg.tradingview_signal_max_feature_age_s):
            return None
        return feat

    def _tv_strong_fade_veto_ok(self, side: str, tv_feature: "dict | None") -> "tuple[bool, str]":
        """Block 1h entries that follow fresh *_STRONG TV (proven negative-alpha cohort)."""
        if not tv_feature:
            return True, ""
        level = str(tv_feature.get("signal_level") or "").strip().upper()
        side = str(side or "").strip().lower()
        if side == "up" and level == "UP_STRONG":
            return False, "tv_strong_fade_veto_up"
        if side == "down" and level == "DOWN_STRONG":
            return False, "tv_strong_fade_veto_down"
        return True, ""

    def _baseline_down_tv_context_ok(self, tv_feature: "dict | None") -> "tuple[bool, str]":
        """Block DOWN in proven-losing bullish TV stacks (15m evening loss cluster)."""
        if not self.cfg.baseline_down_tv_gate_enabled:
            return True, ""
        feat = tv_feature or {}
        mtf = str(feat.get("mtf_alignment") or "").strip().lower()
        range_state = str(feat.get("range_state") or "").strip().lower()
        signal_level = str(feat.get("signal_level") or "").strip().upper()
        volume_state = str(feat.get("volume_state") or "").strip().lower()
        tf_confirm = str(feat.get("tf_confirm") or "").strip().lower()
        bb_state = str(feat.get("bb_state") or "").strip().lower()
        if (self.cfg.baseline_down_block_bb_expansion_down
                and bb_state == "expansion_down"):
            return False, "baseline_down_tv_bb_expansion_down"
        if self.cfg.baseline_down_block_single_tf and tf_confirm == "single_tf":
            return False, "baseline_down_tv_single_tf"
        if self.cfg.baseline_down_block_volume_active and volume_state == "active":
            return False, "baseline_down_tv_volume_active"
        if self.cfg.baseline_down_block_bullish_mtf and mtf == "bullish_aligned":
            return False, "baseline_down_tv_bullish_mtf"
        if (self.cfg.baseline_down_block_up_strong_range_top
                and signal_level == "UP_STRONG" and range_state == "range_top"
                and mtf != "bullish_aligned"):
            return False, "baseline_down_tv_up_strong_range_top"
        if self.cfg.baseline_down_block_bullish_range:
            if mtf == "bullish_aligned" and range_state in ("range_top", "breakout_up"):
                return False, "baseline_down_tv_bullish_range_top"
            if signal_level == "UP_STRONG" and range_state == "breakout_up":
                return False, "baseline_down_tv_up_strong_breakout"
        if self.cfg.baseline_down_block_up_strong_bullish:
            if signal_level == "UP_STRONG" and mtf == "bullish_aligned":
                return False, "baseline_down_tv_up_strong_bullish"
        return True, ""

    def _baseline_up_tv_strength_ok(self, tv_feature: "dict | None") -> "tuple[bool, str]":
        """Baseline UP requires fresh TV UP_STRONG (direction UP, strength >= 0.8)."""
        if not self.cfg.baseline_up_tv_gate_enabled:
            return True, ""
        if not tv_feature:
            return False, "baseline_up_tv_missing"
        direction = str(tv_feature.get("direction") or "").upper()
        if direction != "UP":
            return False, "baseline_up_tv_opposes"
        try:
            strength = float(tv_feature.get("strength"))
        except (TypeError, ValueError):
            return False, "baseline_up_tv_strength_missing"
        if strength < 0.8:
            return False, "baseline_up_tv_weak"
        level = str(tv_feature.get("signal_level") or "").upper()
        if level != "UP_STRONG":
            return False, "baseline_up_tv_not_strong"
        return True, ""

    def _edge_snap_field(self, esnap, field: str):
        if esnap is None:
            return None
        val = getattr(esnap, field, None)
        if val is None and isinstance(esnap, dict):
            val = esnap.get(field)
        return val

    def _edge_snap_ob_pressure(self, esnap) -> "str | None":
        obp = self._edge_snap_field(esnap, "orderbook_pressure")
        if isinstance(obp, dict):
            return obp.get("bucket")
        return None

    def _mispricing_follow_up_ok(self, esnap=None,
                                 tv_feature: "dict | None" = None) -> "tuple[bool, str]":
        """UP mispricing-follow needs TV UP_STRONG + proven Grok UP edge + high score + CEX agree."""
        tv_ok, tv_reason = self._baseline_up_tv_strength_ok(tv_feature)
        if not tv_ok:
            return False, f"misprice_{tv_reason}"
        if not self._grok_up_side_allowed():
            return False, "misprice_up_grok_no_edge"
        bucket = self._edge_snap_field(esnap, "pulse_edge_score_bucket")
        if bucket not in ("high", "very_high"):
            return False, "misprice_up_low_edge_score"
        if self._edge_snap_field(esnap, "cex_agreement_bucket") != "strong":
            return False, "misprice_up_weak_cex_agreement"
        return True, ""

    def _mispricing_follow_entry(self, cex_sig: "dict | None", ttc_s: "float | None",
                                 esnap=None, tv_feature: "dict | None" = None) -> "dict | None":
        """When Grok abstains, follow a confirmed CEX-lead mispricing stack (gates pre-checked)."""
        if not self.cfg.mispricing_follow_on_abstain:
            return None
        cl = cex_sig or {}
        side = cl.get("side")
        if side not in ("up", "down"):
            return None
        if side == "up" and self.cfg.directional_up_restrictions_enabled:
            self._mispricing_gate_counts["misprice_up_side_disabled"] = (
                self._mispricing_gate_counts.get("misprice_up_side_disabled", 0) + 1)
            return None
        mp_ok, _ = self._mispricing_gate_ok(side=side, cex_sig=cl, ttc_s=ttc_s, esnap=esnap)
        et_ok, _ = self._edge_ttc_gate_ok(esnap=esnap, ttc_s=ttc_s)
        if not (mp_ok and et_ok):
            return None
        cex_p = cl.get("cex_p_up")
        if cex_p is None:
            return None
        p_win = float(cex_p) if side == "up" else (1.0 - float(cex_p))
        size_frac = max(0.25, min(1.0, float(self.cfg.mispricing_follow_size_fraction)))
        return {"side": side, "p_win": p_win, "size_frac": size_frac}

    def _mispricing_gate_ok(self, *, side: str, cex_sig: "dict | None", ttc_s: "float | None",
                            esnap=None, window_seconds: int = 300) -> "tuple[bool, str]":
        """Restrict-only: Grok-follow trades require aligned CEX-lead mispricing in the TTC window."""
        if not self.cfg.mispricing_gate_enabled:
            return True, ""
        sig = cex_sig or {}
        if not sig.get("has_signal"):
            return False, "misprice_no_cex_signal"
        try:
            div = abs(float(sig.get("divergence") or 0))
        except (TypeError, ValueError):
            return False, "misprice_no_cex_signal"
        if div < float(self.cfg.cex_lead_min_divergence):
            return False, "misprice_divergence_too_small"
        if str(sig.get("side") or "") != str(side):
            return False, "misprice_side_mismatch"
        if self.cfg.mispricing_require_confirmed and not sig.get("confirmed"):
            return False, "misprice_not_confirmed"
        if ttc_s is None:
            return False, "misprice_ttc_unknown"
        ttc_f = float(ttc_s)
        scale = float(window_seconds or 300) / 300.0
        ttc_min = float(self.cfg.mispricing_ttc_min_s) * scale
        ttc_max = float(self.cfg.mispricing_ttc_max_s) * scale
        if ttc_f < ttc_min or ttc_f > ttc_max:
            return False, "misprice_ttc_out_of_window"
        if side == "down" and self.cfg.mispricing_require_stale_down:
            stale = getattr(esnap, "stale_divergence_class", None) if esnap is not None else None
            if stale is None and isinstance(esnap, dict):
                stale = esnap.get("stale_divergence_class")
            if stale != "stale_polymarket_down":
                return False, "misprice_stale_down_required"
        return True, ""

    def _edge_ttc_gate_ok(self, *, esnap=None, ttc_s: "float | None" = None,
                          window_seconds: int = 300) -> "tuple[bool, str]":
        """Restrict-only: block mid/late TTC unless pulse_edge_score is high or very_high."""
        if not self.cfg.edge_ttc_gate_enabled or ttc_s is None:
            return True, ""
        ttc_f = float(ttc_s)
        scale = float(window_seconds or 300) / 300.0
        mid_lo = 90.0 * scale
        mid_hi = 180.0 * scale
        late_thr = 240.0 * scale
        bucket = self._edge_snap_field(esnap, "pulse_edge_score_bucket")
        if mid_lo <= ttc_f < mid_hi:
            if bucket not in ("high", "very_high"):
                return False, "edge_ttc_mid_window_low_score"
        if ttc_f >= late_thr and bucket not in ("high", "very_high"):
            return False, "edge_ttc_late_window_low_score"
        return True, ""

    def _follow_executable_edge_ok(self, *, p_win: "float | None",
                                   ask: "float | None") -> "tuple[bool, str]":
        """Restrict-only: grok_follow/council must clear a real executable margin (p_win - ask)."""
        if p_win is None or ask is None:
            return False, "follow_executable_unknown"
        margin = float(p_win) - float(ask) - float(self.cfg.edge_buffer)
        thr = float(self.cfg.council_min_executable_margin or self.cfg.min_edge)
        if margin < thr:
            return False, "follow_executable_margin_low"
        return True, ""

    def _executable_mispricing_ok(self, *, p_win: "float | None",
                                  ask: "float | None") -> "tuple[bool, str]":
        """Restrict-only: require p_win - ask - edge_buffer >= min executable margin."""
        if not self.cfg.mispricing_gate_enabled:
            return True, ""
        if p_win is None or ask is None:
            return False, "misprice_executable_unknown"
        margin = float(p_win) - float(ask) - float(self.cfg.edge_buffer)
        if margin < float(self.cfg.mispricing_min_executable_margin):
            return False, "misprice_executable_margin_low"
        return True, ""

    def _tv_signal_gate(self, tv_feature: "dict | None", side: "str | None") -> "str | None":
        """Restrict-only TradingView indication gate. Returns None if the trade is permitted, else
        a rejection reason. Only ACTIVE when the intake exists; it can never force a trade."""
        if not self.cfg.tradingview_signal_gate_enabled or self.tradingview is None:
            return None
        if not tv_feature:
            return "tv_gate_no_signal"            # no fresh TradingView indication -> don't trade
        direction = str(tv_feature.get("direction") or "").upper()
        if direction == "FLAT":
            return "tv_gate_flat_signal"
        want = "up" if direction == "UP" else ("down" if direction == "DOWN" else None)
        if want is None:
            return "tv_gate_no_direction"
        if side != want:
            return "tv_gate_opposes_signal"       # bot side disagrees with the TradingView signal
        min_str = float(self.cfg.tradingview_min_signal_strength or 0.0)
        if min_str > 0:
            try:
                strength = float(tv_feature.get("strength"))
            except (TypeError, ValueError):
                strength = None
            if strength is None or strength < min_str:
                return "tv_gate_weak_signal"
        return None

    def _learning_weight(self) -> "tuple[float, str]":
        """How much the learned edge model influences the directional probability. Influence is
        EARNED (ramps with sample count past the minimum), GATED (only when calibrated), and
        SELF-DISABLING (0 if calibration error exceeds the cap). Returns (weight, reason)."""
        if not self.cfg.learning_enabled or self.edge_model is None:
            return 0.0, "disabled"
        if self.edge_model.n_labeled < self.cfg.learning_min_samples:
            return 0.0, "insufficient_samples"
        ece = self.edge_model.calibration_error()
        if ece is None:
            return 0.0, "calibration_unknown"
        if ece > self.cfg.learning_max_calib_error:
            return 0.0, "calibration_degraded"          # auto-disable a miscalibrated model
        # MARKET-BEATING GATE (kills phantom edge): a calibrated model is not necessarily MORE
        # accurate than the Polymarket price. Only blend when the model's out-of-sample Brier
        # actually beats the market's by the required margin over enough graded windows.
        bench = self._market_benchmark()
        if (bench["n"] >= self.cfg.learning_bench_min_samples
                and bench["model_brier"] is not None and bench["market_brier"] is not None
                and bench["model_brier"] > bench["market_brier"] - self.cfg.learning_bench_margin):
            return 0.0, "model_not_beating_market"
        ramp = max(1.0, float(self.cfg.learning_ramp_samples))
        progress = (self.edge_model.n_labeled - self.cfg.learning_min_samples) / ramp
        weight = self.cfg.learning_max_weight * min(1.0, max(0.0, progress))
        return round(weight, 4), "active"

    def _learning_report(self) -> dict:
        weight, reason = self._learning_weight()
        return {"enabled": bool(self.cfg.learning_enabled),
                "active": weight > 0, "weight": weight, "reason": reason,
                "paper_only": True, "execution_gate_still_authoritative": True,
                "max_weight": self.cfg.learning_max_weight,
                "min_samples": self.cfg.learning_min_samples,
                "ramp_samples": self.cfg.learning_ramp_samples,
                "max_calibration_error": self.cfg.learning_max_calib_error,
                "model_n_labeled": (self.edge_model.n_labeled if self.edge_model else 0),
                "model_calibration_error": (self.edge_model.calibration_error()
                                            if self.edge_model else None),
                "market_benchmark": self._market_benchmark(),
                "note": ("the bot's own settled-trade experience (calibrated edge model) adjusts "
                         "the directional probability; it grows as more trades settle. The "
                         "execution-quality gate, paper-realism, and reconciliation are unchanged "
                         "and still veto every trade — learning can never bypass them.")}

    def _global_reconciliation(self) -> dict:
        from engine.pulse.reconciliation import global_reconciliation
        return global_reconciliation(
            lifecycle=self.reconciler.report(), exec_gate=self.ledger.exec_gate_stats(),
            ledger_stats=self.ledger.stats(), baseline=(self._baseline or empty_baseline()))

    def _gate_thresholds(self) -> dict:
        """The configured execution-gate thresholds (for the zero-reject diagnostic)."""
        return {"size_usd": self.cfg.size_usd, "max_spread": self.cfg.exec_max_spread,
                "min_depth_usd": self.cfg.min_depth_usd,
                "min_order_usd": self.cfg.exec_min_order_usd,
                "max_depth_consume_frac": self.cfg.exec_max_depth_consume_frac,
                "min_ev_after_slippage": self.cfg.exec_min_ev_after_slippage,
                "min_seconds_to_close": self.cfg.min_seconds_to_close,
                "max_book_age_s": self.cfg.exec_max_book_age_s}

    def _ledger_for_report(self) -> dict:
        """Directional ledger for full-report / dashboard aggregation."""
        from engine.pulse.report_epoch import filter_ledger_doc
        raw = self.ledger.to_dict()
        return filter_ledger_doc(raw, self._report_epoch)

    def light_report(self) -> dict:
        """The latest light report (report-only): full lifecycle reconciliation, exec stats,
        reject reasons, EV before/after costs, PnL grouped by every bucket dimension, calibration,
        sample sizes, missing-data reasons, and promotion/demotion candidates."""
        self._repair_accounting_drift()
        from engine.pulse.reporting import build_light_report
        ev_stats = {"n": self._ev_n,
                    "avg_ev_before_costs": (round(self._ev_before_sum / self._ev_n, 6)
                                            if self._ev_n else None),
                    "avg_ev_after_costs": (round(self._ev_after_sum / self._ev_n, 6)
                                           if self._ev_n else None)}
        miss = self.research.report().get("missing_data_reasons", {}) if self.research else {}
        report = build_light_report(
            lifecycle=self.reconciler.report(), execution_gate=self.ledger.exec_gate_stats(),
            ledger_stats=(self._ledger_for_report().get("stats") or self.ledger.stats()),
            calibration=self.calib.to_dict(),
            ev_stats=ev_stats, outcome_groups=self._groups, tier_table=self._tier_report(),
            edge_model=(self.edge_model.report() if self.edge_model else {}),
            sizing={"enabled": self.cfg.sizing_enabled, "actual_size_usd": self.cfg.size_usd},
            missing_data_reasons=miss, baseline=(self._baseline or empty_baseline()),
            gate_thresholds=self._gate_thresholds(), gate_observations=self.gate_obs.ranges())
        report["readiness"] = self.readiness()
        report["tradingview"] = self._tradingview_report()
        report["down_stack"] = self.down_stack.report()
        report["learning"] = self._learning_report()
        report["capital"] = self._capital_status()
        report["grok_signal_intel"] = self._grok_intel_report()
        report["grok_decider"] = self._grok_decider_report()
        report["verifier"] = (self.verifier.report() if self.verifier is not None
                              else {"enabled": False})
        report["research_loop"] = (self.research_loop.report() if self.research_loop is not None
                                   else {"enabled": False})
        if isinstance(report["research_loop"], dict):
            report["research_loop"]["auto_applied_avoid_contexts"] = sorted(self._research_avoid)
            report["research_loop"]["auto_applied_exploit_contexts"] = sorted(self._research_exploit)
        report["lessons"] = self.lessons.report()
        report["loops"] = self._loops_report()
        report["edge_signal"] = self._edge_signal_report()
        report["cex_lead_edge"] = (self.cex_lead.report() if self.cex_lead is not None
                                   else {"enabled": False})
        report["clob_feed"] = (
            self.clob_feed.latency_report() if getattr(self, "clob_feed", None) else {})
        report["walk_forward"] = self._walk_forward_status()
        report["series_architecture"] = {
            "design": "5m_brain_15m_hands",
            "scan_slugs": list(self.cfg.pulse_series_slugs),
            "directional_slugs": list(self.cfg.directional_series_slugs),
        }
        report["profit_discovery"] = self._profit_discovery_status()
        report["five_x_improvement"] = report["profit_discovery"]
        report["directional_risk"] = {
            "directional_enabled": bool(self.cfg.directional_enabled),
            "strategy_mode": ("directional" if self.cfg.directional_enabled else "disabled"),
            "max_bankroll_frac": self.cfg.directional_max_bankroll_frac,
            "bankroll_cap_usd": round(
                float(self.cfg.starting_capital_usd) * float(self.cfg.directional_max_bankroll_frac),
                2),
            "open_exposure_usd": round(self._directional_open_exposure(), 2),
            "block_up_until_promoted": bool(self.cfg.directional_block_up_until_promoted),
            "directional_down_only": bool(self.cfg.directional_down_only),
            "directional_series_slugs": list(self.cfg.directional_series_slugs),
            "research_auto_apply": bool(self.cfg.research_auto_apply),
            "research_forbid_size_increase": bool(self.cfg.research_forbid_size_increase),
            "up_promoted": self._up_direction_promoted(),
        }
        report["directional_allowlist"] = {
            "enabled": bool(self.cfg.directional_require_winning_bucket),
            "explore_rate": self.cfg.directional_explore_rate,
            "explored": self._allowlist_explored, "blocked": self._allowlist_blocked}
        from engine.pulse.reporting import ledger_stats_by_market_series
        led_epoch = self._ledger_for_report()
        report["by_market_series"] = ledger_stats_by_market_series(led_epoch.get("positions"))
        report["markets_feed"] = self._directional_market_feeds_report()
        report["baseline_cohort_gate"] = self._baseline_cohort_gate_report()
        report["learned_selectivity_gate"] = self._selectivity_report()
        report["late_window_entry"] = self._late_window_report()
        report["stop_conditions"] = self.stop_monitor.report()
        from engine.pulse.execution_realistic import aggregate_report
        bench = self._market_benchmark()
        kl_agg = {
            "observe_only": True,
            "latest_model_p": None,
            "latest_market_p": None,
            "kl": None,
            "market_benchmark_n": bench.get("n"),
            "model_beats_market": bench.get("model_beats_market"),
        }
        if bench.get("n"):
            kl_agg["market_benchmark_n"] = bench["n"]
        report["execution_realistic_edge"] = aggregate_report(
            samples=self._exec_realistic_samples,
            payoff_guards=self._payoff_guard_counts,
            kl_aggregate=kl_agg,
        )
        report["simplex_diagnostics"] = self._last_simplex
        from engine.pulse.reporting import build_report_sections
        report["sections"] = build_report_sections(
            report, status={"ticks": self.ticks}, ledger=self._ledger_for_report())
        from engine.pulse.performance_scoring import compute_report_scores
        report["scores"] = compute_report_scores(
            report["sections"], global_reconciled=bool(report.get("global_reconciled")))
        report["score_history"] = self._score_history.to_dict()
        report["loop_synthesis"] = self._loop_synthesis_and_improve(report)
        report["report_epoch"] = dict(self._report_epoch or {})
        if self._report_epoch.get("ts"):
            cap = report.get("capital") or {}
            stats = led_epoch.get("stats") or {}
            cap["realized_pnl_usd"] = stats.get("realized_pnl_usd", cap.get("realized_pnl_usd"))
            cap["total_realized_pnl_usd"] = round(float(stats.get("realized_pnl_usd") or 0), 4)
            report["capital"] = cap
        report["schema"] = "btc_pulse_light_report/1.3"
        return report

    def _loop_synthesis_and_improve(self, report: dict) -> dict:
        """WS5 loop engine: read live report and emit bounded improvement proposals."""
        from engine.pulse.loop_synthesis import synthesize
        synth = synthesize(report)
        self._loop_synthesis_cache = synth
        self.loops.beat("loop_synthesis")
        return synth

    def _late_window_report(self) -> dict:
        """Late-window high-conviction entry mode (gate) + observe-only time-decay edge grade."""
        return {"gate": self.late_window_gate.report(),
                "edge_measurement": self.late_window_edge.report()}

    def _selectivity_report(self) -> dict:
        """Learned Selectivity Gate report: counts, reject reasons, per-decision PnL/win-rate, and
        the counterfactual replay over the existing ledger."""
        return self.selectivity_gate.report(evidence=self.selectivity_evidence,
                                             positions=self._selectivity_positions())

    def _hourly_entry_report(self) -> dict:
        return self.hourly_entry_gate.report(evidence=self.hourly_entry_evidence)

    def _edge_signal_report(self) -> dict:
        """Observe-only BTC Pulse Edge Signal report (CEX coverage, bucketed PnL/win/EV,
        best/worst-after-cost, promotion diagnostics)."""
        if self.edge_signal is None:
            return {"enabled": False, "observe_only": True, "affects_trading": False}
        return {"enabled": True, **self.edge_signal.report(
            now=self.last_tick_ts or time.time(),
            promotion_allowed=self.cfg.edge_promotion_allowed,
            min_samples=self.cfg.edge_promotion_min_samples,
            min_win_rate=self.cfg.edge_promotion_min_win_rate)}

    def _grok_analyst_report(self) -> dict:
        """Snapshot the bot's GROWING learned evidence for the Grok batch analyst (observe-only), so
        Grok learns the bot's trading patterns and scrubs the data better as the bot accumulates
        experience: settled-trade bucket performance, the learned-selectivity bucket evidence
        (win-rate vs its own breakeven + confidence), the late-window time-decay edge, gate stats,
        edge-model calibration, and the TradingView signal learning."""
        try:
            rep = {"signal_learning": self._tv_learner.report(
                        promotion_allowed=self.cfg.tradingview_promotion_allowed,
                        min_samples=self.cfg.tradingview_promotion_min_samples,
                        min_win_rate=self.cfg.tradingview_promotion_min_win_rate),
                   "edge_vs_5min_outcome": self._tv_edge.report(),
                   "rsi_trend": self._rsi_model.report(),
                   "ledger": self.ledger.stats()}
            # the bot's OWN learned trading patterns (this is what makes Grok "grow with the bot")
            rep["learned_pnl_by_bucket"] = self._groups.summary()
            rep["learned_selectivity"] = self.selectivity_gate.report(
                evidence=self.selectivity_evidence, positions=self._selectivity_positions())
            rep["late_window_edge"] = self.late_window_edge.report()
            rep["context_gate"] = self.tv_context_gate.report()
            rep["reward_risk_floor"] = {"min_reward_risk": self.cfg.min_reward_risk}
            if self.edge_model is not None:
                em = self.edge_model.report()
                rep["edge_model"] = {"n_labeled": em.get("n_labeled"),
                                     "calibration_error": em.get("calibration_error"),
                                     "calibration_table": em.get("calibration_table")}
            return rep
        except Exception:  # noqa: BLE001
            return {}

    def _record_lessons_from_settlement(self, pos) -> None:
        """Turn proven outcomes into compounding rules (deduped): avoid confidently-losing buckets,
        exploit proven-edge contexts, and note breaker trips. Fed back to maker + checker."""
        try:
            new_lesson = False
            now = self.last_tick_ts or time.time()
            self.loops.beat("lessons", now)
            self.loops.beat("risk_monitor", now)
            active_keys = set()              # (kind,key) currently evidence-backed -> not retracted
            be = self.selectivity_gate.bucket_evidence(self.selectivity_evidence, top=8)
            for r in be.get("buckets", []):
                if r.get("confidently_losing"):
                    key = "sel:%s=%s" % (r["dimension"], r["bucket"])
                    active_keys.add(("avoid", key))
                    new_lesson |= self.lessons.add(
                        kind="avoid", key=key,
                        rule=("AVOID %s=%s — confidently below breakeven (WR %s vs %s, n %s, "
                              "EV/trade %s)." % (r["dimension"], r["bucket"], r.get("win_rate"),
                              r.get("breakeven_win_rate"), r.get("n"), r.get("ev_per_trade"))), now=now)
            if self.grok_decider is not None:
                for c in (self.grok_decider.report().get("view_edge_candidates") or []):
                    key = "edge:%s=%s" % (c["dimension"], c["bucket"])
                    active_keys.add(("exploit", key))
                    if self.lessons.add(
                            kind="exploit", key=key,
                            rule=("EXPLOIT %s=%s — Grok's directional view is a real edge (acc %s, "
                                  "lowerCI %s, n %s)." % (c["dimension"], c["bucket"], c.get("accuracy"),
                                  c.get("accuracy_lower_ci"), c.get("n"))), now=now):
                        new_lesson = True
                        if self.research_loop is not None:
                            self.research_loop.request_run("new_edge")
                br = self.grok_decider.breaker_status()
                if br.get("tripped"):
                    day = int((self.last_tick_ts or time.time()) // 86400)
                    if self.lessons.add(kind="risk", key="breaker:%s:%s" % (br.get("reason"), day),
                                        rule="Circuit breaker tripped (%s) — follow paused, baseline "
                                             "only." % br.get("reason")):
                        new_lesson = True
                        if self.research_loop is not None:
                            self.research_loop.request_run("breaker")
            # RETRACT avoid/exploit lessons no longer backed by live evidence (regime changed) so the
            # maker/checker stop reading stale rules. Risk (breaker) lessons are historical, not synced.
            retracted = self.lessons.sync(active_keys=active_keys, now=now).get("n", 0)
            # event-trigger the research meta-loop on any new/retracted lesson + a fresh-sample cadence
            if self.research_loop is not None:
                if new_lesson or retracted:
                    self.research_loop.request_run("new_lesson" if new_lesson else "lesson_retracted")
                elif int(self.ledger.stats().get("settled", 0) or 0) % 15 == 0:
                    self.research_loop.request_run("fresh_samples")
        except Exception:  # noqa: BLE001 — lessons never break settlement
            pass

    def _research_report(self) -> dict:
        """Compact report for the research meta-loop: full light report + the compounding lessons."""
        try:
            rep = self.light_report()
            rep["lessons"] = self.lessons.recent(20)
            return rep
        except Exception:  # noqa: BLE001
            return {"lessons": self.lessons.recent(20)}

    # research dimensions -> selectivity-tag dimensions (so Claude's avoid_contexts map to live tags)
    _RESEARCH_DIM_ALIAS = {"regime": "hurst_regime", "hurst": "hurst_regime",
                           "edge_quality": "edge_quality_bucket", "confidence": "confidence_tier",
                           "spread": "spread_bucket", "depth": "depth_bucket",
                           "zscore": "zscore_bucket", "ttc": "ttc_bucket"}
    # NOTE: "direction" is EXCLUDED (a whole side is too coarse); "depth_bucket"/"spread_bucket" are
    # EXCLUDED too — they are liquidity ATTRIBUTES, not directional edge contexts, and blocking them
    # (e.g. depth>=1000 = most of the book) would freeze nearly all trading. We avoid losing
    # directional CONTEXTS only.
    _RESEARCH_AVOID_DIMS = {"hurst_regime", "zscore_bucket", "ttc_bucket", "confidence_tier",
                            "markov_state", "edge_quality_bucket", "stale_divergence"}

    def _research_rule_evidence_backed(self, dim: str, bucket: str) -> bool:
        """MAKER-CHECKER: only auto-apply a Claude-proposed avoid-rule if the bot's OWN live evidence
        CONFIDENTLY proves that bucket is losing (Wilson upper bound < its breakeven + net-negative),
        the SAME bar the selectivity gate uses. This grounds the self-improving loop in data and stops
        the LLM from hallucinating / over-broad blocks (e.g. a confidence tier that doesn't exist)."""
        try:
            st = self.selectivity_evidence.stat(dim, bucket)
            if not st or st["n"] < self.selectivity_gate.min_samples:
                return False
            return bool(self.selectivity_gate._assess(st).get("confidently_losing"))
        except Exception:  # noqa: BLE001
            return False

    def _research_exploit_backed(self, dim: str, bucket: str) -> bool:
        """MAKER-CHECKER for the EXPLOIT side: only promote a Claude-proposed exploit-context if the
        bot's OWN data CONFIDENTLY proves it WINNING — Wilson LOWER bound of win-rate above the
        bucket's own breakeven AND net-positive PnL. Mirrors the avoid checker; grounds size-ups in
        evidence, never opinion."""
        try:
            from engine.pulse.cex_lead import _wilson_lower
            from engine.pulse.selectivity import breakeven_win_rate
            st = self.selectivity_evidence.stat(dim, bucket)
            if not st or st["n"] < self.selectivity_gate.min_samples or st["pnl_usd"] <= 0:
                return False
            n = int(st["n"]); wins = int(round(float(st["win_rate"]) * n))
            wl = _wilson_lower(wins, n, self.selectivity_gate.confidence_z)
            be = breakeven_win_rate(st["avg_win"], st["avg_loss"])
            return wl is not None and wl > be
        except Exception:  # noqa: BLE001
            return False

    def _research_apply(self, note: dict) -> list:
        """Bounded, evidence-gated, SAFETY-only auto-apply of the research loop's avoid_contexts:
        turn a Claude proposal into a hard block ONLY when the bot's own data confirms it is
        confidently losing (maker-checker). Only-more-selective, capped, deduplicated; never loosens a
        gate, changes size, enables live, or applies exploit/knob nudges. Closes the self-improving
        loop on EVIDENCE, not opinion."""
        applied = []
        for ctx in (note.get("avoid_contexts") or []):
            if "=" not in str(ctx):
                continue
            dim, _, bucket = str(ctx).partition("=")
            dim = dim.strip().lower()
            bucket = bucket.strip()
            for sep in (" (", "(", " "):                     # drop any prose the model appended
                if sep in bucket:
                    bucket = bucket.split(sep, 1)[0]
            bucket = bucket.strip().strip(",").strip().lower()   # tags are lowercase
            cdim = self._RESEARCH_DIM_ALIAS.get(dim, dim)
            if cdim not in self._RESEARCH_AVOID_DIMS or not bucket:
                continue
            if not self._research_rule_evidence_backed(cdim, bucket):   # maker-checker on live data
                continue
            key = "%s=%s" % (cdim, bucket)
            if key not in self._research_avoid and len(self._research_avoid) < self.cfg.research_avoid_max:
                self._research_avoid.add(key)
                applied.append(key)
        # EXPLOIT side (dual of avoid): promote Claude exploit-contexts that the data proves WINNING
        for ctx in (note.get("exploit_contexts") or []):
            if "=" not in str(ctx):
                continue
            dim, _, bucket = str(ctx).partition("=")
            dim = dim.strip().lower()
            bucket = bucket.strip()
            for sep in (" (", "(", " "):
                if sep in bucket:
                    bucket = bucket.split(sep, 1)[0]
            bucket = bucket.strip().strip(",").strip().lower()
            cdim = self._RESEARCH_DIM_ALIAS.get(dim, dim)
            if cdim not in self._RESEARCH_AVOID_DIMS or not bucket:
                continue
            if not self._research_exploit_backed(cdim, bucket):
                continue
            key = "%s=%s" % (cdim, bucket)
            if (key not in self._research_exploit
                    and len(self._research_exploit) < self.cfg.research_exploit_max):
                self._research_exploit.add(key)
                applied.append("exploit:" + key)
        return applied

    def _directional_window_feeds(self) -> tuple:
        return tuple(
            feed for feed in (
                self._directional_hourly_feed,
                getattr(self, "_directional_15m_feed", None),
            )
            if feed is not None
        )

    @staticmethod
    def _is_15m_window(w) -> bool:
        ws = int(getattr(w, "window_seconds", 0) or 0)
        if 600 <= ws <= 1200:
            return True
        slug = str(getattr(w, "series_slug", "") or "").lower()
        label = str(getattr(w, "series_label", "") or "").lower()
        return "15m" in slug or label.endswith("_15m")

    def _tv_15m_chart_lean_for_window(self, w) -> dict:
        """Dual-horizon OHLC lean from bar-close FIFO (lane-routed INDEX *USD on 15m)."""
        empty = {"trade_lean": None, "alignment": "none", "confidence": "none",
                 "short_n": 0, "regime_n": 0}
        if self.tradingview is None:
            return empty
        if not bool(getattr(self.cfg, "tv_15m_chart_lean_enabled", True)):
            return {**empty, "enabled": False}
        try:
            from engine.pulse.tradingview import tv_symbol_for_window
            from engine.pulse.tv_15m_price_path import (
                compact_path_for_plot, dual_horizon_price_path, resolve_bar_close_from_intake,
                trade_lean_from_path)
            requested = tv_symbol_for_window(w) or self.cfg.tradingview_feature_symbol
            sym, alerts = resolve_bar_close_from_intake(self.tradingview, requested)
            regime_n = max(1, int(self.cfg.tradingview_alert_history_per_symbol or 50))
            short_n = max(6, min(int(getattr(self.cfg, "tv_15m_short_path_n", 8) or 8), regime_n))
            dual = dual_horizon_price_path(alerts, regime_n=regime_n, short_n=short_n)
            lean = trade_lean_from_path(dual)
            lean["enabled"] = True
            lean["symbol"] = sym
            lean["requested_symbol"] = requested
            lean["path_tf"] = "5m"
            lean["price_pattern"] = compact_path_for_plot(dual)
            return lean
        except Exception:  # noqa: BLE001
            return empty

    def _tv_hourly_chart_lean_for_window(self, w) -> dict:
        """1h dual-horizon lean: short=last 12 × 5m bar-close (~1h), regime=last 50."""
        empty = {"trade_lean": None, "alignment": "none", "confidence": "none",
                 "short_n": 0, "regime_n": 0}
        if self.tradingview is None:
            return empty
        if not bool(getattr(self.cfg, "tv_1h_chart_lean_enabled", True)):
            return {**empty, "enabled": False}
        try:
            from engine.pulse.tradingview import tv_symbol_for_window
            from engine.pulse.tv_15m_price_path import (
                compact_path_for_plot, dual_horizon_price_path, resolve_bar_close_from_intake,
                trade_lean_from_path)
            requested = tv_symbol_for_window(w) or self.cfg.tradingview_feature_symbol
            sym, alerts = resolve_bar_close_from_intake(self.tradingview, requested)
            regime_n = max(1, int(self.cfg.tradingview_alert_history_per_symbol or 50))
            short_n = max(6, min(int(getattr(self.cfg, "tv_1h_short_path_n", 12) or 12), regime_n))
            dual = dual_horizon_price_path(alerts, regime_n=regime_n, short_n=short_n)
            lean = trade_lean_from_path(dual)
            lean["enabled"] = True
            lean["symbol"] = sym
            lean["requested_symbol"] = requested
            lean["lane"] = "1h"
            lean["path_tf"] = "5m"
            lean["price_pattern"] = compact_path_for_plot(dual)
            return lean
        except Exception:  # noqa: BLE001
            return empty

    def _tv_rsi_overlay_for_window(self, w, now: float) -> Optional[dict]:
        if self.tradingview is None:
            return None
        if not bool(getattr(self.cfg, "tv_rsi_overlay_enabled", True)):
            return None
        try:
            from engine.pulse.tradingview import tv_symbol_for_window
            from engine.pulse.tv_rsi_overlay import resolve_rsi_overlay_from_intake
            requested = tv_symbol_for_window(w) or self.cfg.tradingview_feature_symbol
            return resolve_rsi_overlay_from_intake(
                self.tradingview, requested, now=float(now),
                max_age_s=float(getattr(self.cfg, "tv_rsi_overlay_max_age_s", 2700.0) or 2700.0))
        except Exception:  # noqa: BLE001
            return None

    def _tv_rsi_band_for_window(self, w, now: float) -> Optional[dict]:
        if self.tradingview is None:
            return None
        if not bool(getattr(self.cfg, "tv_rsi_band_enabled", True)):
            return None
        try:
            from engine.pulse.tradingview import tv_symbol_for_window
            from engine.pulse.tv_rsi_band import resolve_rsi_band_from_intake
            requested = tv_symbol_for_window(w) or self.cfg.tradingview_feature_symbol
            return resolve_rsi_band_from_intake(
                self.tradingview, requested, now=float(now),
                max_age_s=float(getattr(self.cfg, "tv_rsi_band_max_age_s", 900.0) or 900.0))
        except Exception:  # noqa: BLE001
            return None

    def _tv_rsi_divergence_for_window(self, w, now: float) -> Optional[dict]:
        if self.tradingview is None:
            return None
        if not bool(getattr(self.cfg, "tv_rsi_divergence_analysis_enabled", True)):
            return None
        try:
            from engine.pulse.tradingview import tv_symbol_for_window
            from engine.pulse.tv_rsi_divergence import resolve_rsi_divergence_from_intake
            requested = tv_symbol_for_window(w) or self.cfg.tradingview_feature_symbol
            return resolve_rsi_divergence_from_intake(
                self.tradingview, requested, now=float(now),
                max_age_s=float(getattr(self.cfg, "tv_rsi_overlay_max_age_s", 2700.0) or 2700.0))
        except Exception:  # noqa: BLE001
            return None

    def _hourly_chart_lean_entry_ok(self, w, side: str, now: float) -> tuple[bool, str, dict]:
        """Shared 1h chart-lean hard gate for legacy tick + Osmani execute."""
        from engine.pulse.hourly_entry_timing import is_hourly_window
        from engine.pulse.tv_15m_price_path import hourly_chart_lean_entry_ok
        ws = int(getattr(w, "window_seconds", 300) or 300)
        if not is_hourly_window(ws):
            return True, "not_hourly", {}
        if not bool(getattr(self.cfg, "tv_1h_chart_lean_gate", True)):
            return True, "gate_disabled", {}
        lean = self._tv_hourly_chart_lean_for_window(w)
        sso = float(w.seconds_since_open(now))
        ok, reason = hourly_chart_lean_entry_ok(
            side=side,
            lean=lean,
            seconds_since_open=sso,
            min_short_n=max(6, int(getattr(self.cfg, "tv_1h_short_path_n", 12) or 12)),
            min_sso_s=float(self.cfg.hourly_min_seconds_since_open),
            gate_enabled=True,
        )
        return ok, reason, lean

    def _directional_windows(self, now: float, *, require_open: bool = False) -> list:
        windows = []
        seen: set[str] = set()
        btc_spot = self.price.current() if self.price is not None else None
        eth_spot = None
        if self._eth_price is not None:
            eth_spot = self._eth_price.current()
        if eth_spot is None:
            eth_spot = (getattr(self.leads, "_latest", {}) or {}).get(
                "binance_ethusdt", (None,))[0]
        if self._directional_hourly_feed is not None:
            for w in self._directional_hourly_feed.active_windows(
                    now=now, btc_spot=btc_spot, eth_spot=eth_spot):
                if require_open and not w.is_open(now):
                    continue
                if w.event_id not in seen:
                    windows.append(w)
                    seen.add(w.event_id)
        feed_15m = getattr(self, "_directional_15m_feed", None)
        if feed_15m is not None:
            for w in feed_15m.active_windows(now=now):
                if require_open and not w.is_open(now):
                    continue
                if w.event_id not in seen:
                    windows.append(w)
                    seen.add(w.event_id)
        windows.sort(key=lambda w: w.close_ts)
        return windows

    def _directional_fair_anchor(self, w, snap) -> Optional[float]:
        """Digital-model anchor: strike for above markets, spot-at-open for up/down."""
        if getattr(w, "market_kind", "") == "above":
            strike = getattr(w, "strike_price", None)
            if strike is not None:
                return float(strike)
        return snap.price if snap is not None else None

    def _needs_eth_oracle(self) -> bool:
        """True when any ETH directional window can appear -> build a dedicated ETH price oracle."""
        slugs = tuple(str(s).lower() for s in (getattr(self.cfg, "directional_series_slugs", ()) or ()))
        slugs += tuple(str(s).lower() for s in (getattr(self.cfg, "directional_event_slugs", ()) or ()))
        if getattr(self.cfg, "directional_hourly_discover", True):
            slugs += ("eth-up-or-down-hourly",)
        if getattr(self.cfg, "directional_15m_discover", True):
            slugs += ("eth-up-or-down-15m",)
        return any(("eth" in s or "ethereum" in s) for s in slugs)

    @staticmethod
    def _window_asset(w) -> str:
        """Underlying asset for a window ('eth' | 'btc'). ETH directional windows carry an eth series
        slug/label; everything else defaults to the BTC oracle."""
        slug = str(getattr(w, "series_slug", "") or "").lower()
        label = str(getattr(w, "series_label", "") or "").lower()
        if slug.startswith("eth") or "ethereum" in slug or label.startswith("eth"):
            return "eth"
        return "btc"

    def _price_feed_for(self, w):
        """Route by BOTH horizon and asset so the model matches the contract's resolution source."""
        is_hourly = int(getattr(w, "window_seconds", 0) or 0) >= 3600
        if is_hourly:
            eth_hourly = getattr(self, "_eth_hourly_price", None)
            btc_hourly = getattr(self, "_btc_hourly_price", None)
            if self._window_asset(w) == "eth" and eth_hourly is not None:
                return eth_hourly
            if self._window_asset(w) == "btc" and btc_hourly is not None:
                return btc_hourly
        if self._eth_price is not None and self._window_asset(w) == "eth":
            return self._eth_price
        return self.price

    def _settle_price_feed_for(self, pos):
        """Capture CLOSE on the same asset+horizon source frozen at entry."""
        rt = pos.research or {}
        is_hourly = int(rt.get("window_seconds") or 0) >= 3600
        if is_hourly:
            eth_hourly = getattr(self, "_eth_hourly_price", None)
            btc_hourly = getattr(self, "_btc_hourly_price", None)
            if str(rt.get("asset") or "") == "eth" and eth_hourly is not None:
                return eth_hourly
            if str(rt.get("asset") or "") == "btc" and btc_hourly is not None:
                return btc_hourly
        if self._eth_price is not None and str((pos.research or {}).get("asset") or "") == "eth":
            return self._eth_price
        return self.price

    def _hydrate_window_books(self, w):
        for feed in self._directional_window_feeds():
            if feed.owns(w):
                return feed.hydrate_books(w)
        return w

    def _find_directional_window(self, event_id: str, now: Optional[float] = None):
        ts = float(now) if now is not None else time.time()
        for win in self._directional_windows(ts):
            if getattr(win, "event_id", None) == event_id:
                return self._hydrate_window_books(win)
        return None

    def _directional_market_feeds_report(self) -> dict:
        feed_15m = getattr(self, "_directional_15m_feed", None)
        return {
            "hourly": (self._directional_hourly_feed.report()
                        if self._directional_hourly_feed is not None else {"enabled": False}),
            "m15": (feed_15m.report() if feed_15m is not None else {"enabled": False}),
        }

    def _gamma_feed_resolve(self, market_id: str) -> Optional[bool]:
        for feed in self._directional_window_feeds():
            res = feed.fetch_resolution(market_id)
            if res is not None:
                return res
        return None

    def _directional_series_allowed(self, w) -> bool:
        """Directional trades only on configured event slugs or explicit directional feeds."""
        if getattr(w, "directional_lane", False):
            return True
        explicit = tuple(self.cfg.directional_event_slugs or ())
        if explicit:
            return str(getattr(w, "slug", "") or "") in explicit
        allowed = tuple(self.cfg.directional_series_slugs or ())
        if not allowed:
            explicit_feeds = bool(
                self.cfg.directional_hourly_discover
                or self.cfg.directional_event_slugs
            )
            return not explicit_feeds
        return str(getattr(w, "series_slug", "") or "") in allowed

    def _directional_up_blocked(self, side: Optional[str]) -> tuple:
        """Return (blocked, reason) for directional UP — no grok/cex bypass when down_only."""
        if str(side or "").lower() != "up":
            return False, ""
        if bool(getattr(self.cfg, "directional_down_only", False)):
            return True, "directional_down_only"
        if (self.cfg.directional_block_up_until_promoted
                and not self._up_direction_promoted()):
            return True, "up_blocked_until_promoted"
        return False, ""

    def _directional_open_exposure(self) -> float:
        exp = 0.0
        for pos in self.ledger.positions.values():
            if pos.status == "open":
                exp += float(getattr(pos, "size_usd", 0.0) or 0.0)
        return exp

    def _btc_correlated_exposure(self, side, now: Optional[float] = None) -> float:
        """Same-direction BTC exposure on still-LIVE windows across lanes -- so the lanes don't stack
        the same directional bet. For a directional ``side`` this sums open directional positions on
        that side only.

        Only counts positions whose window has NOT closed yet (``close_ts > now``): a position past its
        close is resolving/awaiting-settlement -- its window already determined its outcome, so it is
        no longer forward exposure to a NEW window's move (and a stuck/unsettled position must not
        permanently pin the cap). Read-only; only ever BLOCKS a new entry, never forces one."""
        import time as _time
        now = float(now) if now else _time.time()
        side = str(side or "").lower()
        exp = 0.0
        for pos in self.ledger.positions.values():
            if getattr(pos, "status", None) == "open" \
                    and str(getattr(pos, "side", "")).lower() == side \
                    and float(getattr(pos, "close_ts", 0.0) or 0.0) > now:
                exp += float(getattr(pos, "size_usd", 0.0) or 0.0)
        return round(exp, 6)

    def _up_direction_promoted(self) -> bool:
        """True when direction=up bucket clears Wilson LB promotion (n>=min, PnL>0)."""
        return self._research_exploit_backed("direction", "up")

    def _wire_clob_feed_metrics(self) -> None:
        """Record REST book fetch latency on the CLOB feed dashboard."""
        feed = getattr(self, "clob_feed", None)
        if feed is None:
            return

        def _on_fetch(token_id: str, elapsed_ms: float) -> None:
            feed.record_fetch(token_id, elapsed_ms)

        for directional_feed in self._directional_window_feeds():
            if hasattr(directional_feed, "on_book_fetch"):
                directional_feed.on_book_fetch = _on_fetch

    def _ab_experiment_status(self) -> dict:
        from engine.pulse.favorites_policy import (
            ab_profile_from_env,
            favorites_policy_active,
            ledger_ab_stats,
            min_entry_price_from_env,
        )
        offline = None
        try:
            rep_path = self._data_dir / "offline_walk_forward_report.json"
            if rep_path.exists():
                import json
                offline = json.loads(rep_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            offline = None
        return {
            "active_profile": ab_profile_from_env(),
            "favorites_policy_active": favorites_policy_active(),
            "min_entry_price": min_entry_price_from_env(),
            "cell_phase2_enabled": bool(self.cfg.cell_learning_phase2_enabled),
            "cell_learning_cells": (
                len(getattr(self.cell_learning, "cells", {}) or {})
                if getattr(self, "cell_learning", None) is not None else 0),
            "by_profile": ledger_ab_stats(self.ledger.positions),
            "offline_holdout_favorites": (
                (offline or {}).get("holdout", {}).get("favorites")),
            "recommendation": (offline or {}).get("recommendation"),
        }

    def _walk_forward_status(self) -> dict:
        try:
            from engine.pulse.walk_forward import passes_walk_forward
            return {
                "directional": passes_walk_forward(list(self.ledger.positions.values())),
            }
        except Exception:
            return {}

    def _profit_discovery_status(self) -> dict:
        """5x improvement tracker vs baseline; honest status only."""
        baseline_total = float(getattr(self, "_profit_baseline_usd", 35.95) or 35.95)
        ls = self.ledger.stats()
        dir_pnl = float(ls.get("realized_pnl_usd") or 0.0)
        total = dir_pnl
        ratio = (total / baseline_total) if baseline_total > 0 else None
        target = 5.0
        proven = bool(ratio is not None and ratio >= target)
        blockers = []
        if ratio is None or ratio < target:
            blockers.append("total_pnl_below_5x_baseline")
        if int(ls.get("settled") or 0) < 8:
            blockers.append("insufficient_directional_sample")
        primary = "directional" if total > 0 else self.cfg.primary_edge_source
        return {"five_x_target": target, "baseline_total_pnl_usd": baseline_total,
                "current_total_pnl_usd": round(total, 4),
                "directional_pnl_usd": round(dir_pnl, 4),
                "improvement_ratio": (round(ratio, 4) if ratio is not None else None),
                "five_x_improvement_status": ("proven" if proven else "not_proven_yet"),
                "primary_edge_source": primary,
                "top_blockers": blockers[:3]}

    def _directional_market_benchmark_ok(self) -> bool:
        """Directional allowlist requires model Brier <= market Brier once enough graded windows."""
        bench = self._market_benchmark()
        n = int(bench.get("n") or 0)
        if n < self.cfg.learning_bench_min_samples:
            return True
        if bench.get("model_brier") is None or bench.get("market_brier") is None:
            return True
        return bool(bench.get("model_beats_market"))

    def _any_winning_bucket(self, sel_tags: dict) -> bool:
        """True if ANY of the candidate's buckets is CONFIDENTLY WINNING (Wilson lower-bound win-rate
        above its breakeven, n>=min) per live evidence — the directional allowlist (Roan/loop-eng:
        only trade proven edges, not opinion). Reuses the same maker-checker test as research-exploit."""
        for dim, val in (sel_tags or {}).items():
            if dim == "direction" or val is None:
                continue
            if self._research_exploit_backed(dim, str(val)):
                return True
        return False

    def _research_exploit_hit(self, sel_tags: dict) -> bool:
        """True if a candidate's context matches a proven-winning research exploit-rule (never on
        'direction'). Used to SIZE UP proven-winning contexts (capped)."""
        if not self._research_exploit:
            return False
        for dim, val in (sel_tags or {}).items():
            if dim != "direction" and val is not None and (
                    "%s=%s" % (dim, str(val).lower())) in self._research_exploit:
                return True
        return False

    def _research_avoid_hit(self, sel_tags: dict):
        """Return the first sel_tag that matches an active research avoid-rule, else None. Never
        blocks on 'direction' (a whole side is too coarse); matching is case-insensitive."""
        if not self._research_avoid:
            return None
        for dim, val in (sel_tags or {}).items():
            if dim == "direction" or val is None:
                continue
            if ("%s=%s" % (dim, str(val).lower())) in self._research_avoid:
                return "%s=%s" % (dim, str(val).lower())
        return None

    # -- Osmani 2026 loop engineering (3 lanes + maker-checker) ---------------- #
    def _directional_trade_authority_osmani(self, w=None) -> bool:
        """When True, directional fills are owned by Osmani lanes — tick() observes only."""
        if w is not None and getattr(w, "directional_lane", False):
            return False
        return bool(
            self.osmani_loop is not None
            and self.cfg.osmani_loop_enabled
            and not self.cfg.directional_legacy_tick
        )

    def _osmani_tv_feature(self, window, symbol: str, now: float) -> Optional[dict]:
        """Latest TV momentum feature for asset triage skill (asset-matched symbol).

        Freshness uses tradingview_signal_max_feature_age_s (set to match
        PULSE_TRIAGE_TV_MAX_AGE_S=3600 in apply-loop-arch-env so Osmani and triage agree).
        """
        if self.tradingview is None or not symbol:
            return None
        feat = self.tradingview.latest_feature(now=now, symbol=symbol)
        if feat is None:
            return None
        tf = str(feat.get("timeframe") or "")
        if tf not in tuple(self.cfg.tradingview_mtf_timeframes or ("5", "15", "30", "60", "240", "1440")):
            return None
        age = feat.get("age_s")
        if age is not None and float(age) > float(self.cfg.tradingview_signal_max_feature_age_s):
            return None
        return feat

    def _osmani_trend_feature(self, window, symbol: str, now: float) -> Optional[dict]:
        """Trend feature for Osmani triage: spot price_action (default) or legacy TV UP/DOWN."""
        src = (self.cfg.triage_trend_source or "price").strip().lower()
        if src == "tv":
            return self._osmani_tv_feature(window, symbol, now)
        from engine.pulse.price_action_trend import to_triage_feature, trend_for_window
        pf = self._price_feed_for(window)
        raw = trend_for_window(
            window=window,
            price_feed=pf,
            now=now,
            max_price_age_s=float(self.cfg.price_max_age_s),
            min_move_bps=float(self.cfg.price_trend_min_move_bps),
        )
        return to_triage_feature(raw) if raw else None

    def _grok_price_action_trend(self, w, now: float) -> Optional[dict]:
        """BTC + ETH spot trends (rising/falling/flat) for Grok — no TV UP/DOWN labels."""
        from engine.pulse.price_action_trend import dual_asset_snapshot, trend_for_window
        btc_w = eth_w = None
        try:
            for win in self._directional_windows(now, require_open=True):
                slug = str(getattr(win, "series_slug", "") or "").lower()
                if slug.startswith("btc") and btc_w is None:
                    btc_w = win
                elif slug.startswith("eth") and eth_w is None:
                    eth_w = win
        except Exception:  # noqa: BLE001
            pass
        if getattr(w, "series_slug", "").lower().startswith("eth"):
            eth_w = eth_w or w
        else:
            btc_w = btc_w or w
        snap = dual_asset_snapshot(
            btc_feed=self.price,
            eth_feed=self._eth_price,
            btc_window=btc_w,
            eth_window=eth_w,
            now=now,
            max_price_age_s=float(self.cfg.price_max_age_s),
            min_move_bps=float(self.cfg.price_trend_min_move_bps),
        )
        snap["window_asset"] = self._window_asset(w)
        snap["window_trend"] = trend_for_window(
            window=w,
            price_feed=self._price_feed_for(w),
            now=now,
            max_price_age_s=float(self.cfg.price_max_age_s),
            min_move_bps=float(self.cfg.price_trend_min_move_bps),
        )
        snap["note"] = ("rising/falling/flat from Chainlink spot vs window open; "
                         "TradingView UP/DOWN not used when grok_trend_source=price")
        return snap

    def _osmani_directional_windows(self, now: float) -> list:
        """Directional series windows for discovery lane sweet-spot scan."""
        allowed = set(self.cfg.directional_series_slugs or ())
        out = []
        for w in self._directional_windows(now, require_open=True):
            if allowed and getattr(w, "series_slug", "") not in allowed:
                continue
            self._hydrate_window_books(w)
            out.append(w)
        return out

    def _osmani_fair_p(self, w, now: float):
        """Fair P(up) for discovery — digital model + overlay blackout (asset-matched oracle).

        ETH hourly windows MUST use the ETH Chainlink feed (not BTC). Using BTC here made
        ETH open snapshots miss → fair fell back to book mid → edge≈0 → ETH never emitted.
        """
        self._hydrate_window_books(w)
        ov = self.overlay.current(now) if self.overlay is not None else None
        if ov and ov.get("blackout"):
            return None
        _pf = self._price_feed_for(w)
        # Ensure open snapshot exists on the correct asset feed (ETH windows → eth oracle).
        _pf.snapshot_open(w.event_id, w.open_ts, now=now)
        s_now = _pf.current()
        sigma = _pf.sigma_per_sec(now)
        snap = _pf.open_snapshot(w.event_id)
        if s_now is None or sigma is None or snap is None:
            if w.up_book and w.up_book.mid is not None:
                return float(w.up_book.mid)
            return None
        if not _pf.is_fresh(self.cfg.price_max_age_s, now):
            return None
        ov_vol_mult = float(ov.get("vol_multiplier", 1.0)) if ov else 1.0
        ttc = w.seconds_to_close(now)
        return digital_p_up(s_now, snap.price, sigma * ov_vol_mult, ttc)

    def _osmani_hydrate_snapshot(self, snap: dict):
        """Independent API book re-fetch for evaluator (isolated from hot-path state)."""
        eid = snap.get("event_id")
        w = self._find_directional_window(eid)
        if w is None:
            raise ValueError(f"osmani: window {eid} not active for independent verify")
        return w

    def _osmani_hourly_entry_check(self, w, now: float) -> dict:
        """1h entry-timing gate shared by Osmani discovery + execute (same floor as legacy tick)."""
        from engine.pulse.hourly_entry_timing import hourly_entry_bucket
        ws = int(getattr(w, "window_seconds", 300) or 300)
        sso = float(w.seconds_since_open(now))
        bucket = hourly_entry_bucket(sso, window_seconds=ws)
        res = self.hourly_entry_gate.evaluate(
            window_seconds=ws, seconds_since_open=sso,
            evidence=self.hourly_entry_evidence)
        return {**res, "bucket": res.get("bucket") or bucket, "seconds_since_open": round(sso, 1)}

    def _osmani_execute_verified(self, proposal, verified, snapshot) -> bool:
        """Place paper fill after evaluator + Claude verifier + risk caps confirm."""
        if not self.cfg.directional_enabled:
            return False
        if self.stop_monitor.is_halted("directional"):
            return False
        if self.verifier is not None:
            vv = self.verifier.get(proposal.event_id)
            if vv and not vv.get("pending") and not vv.get("approve"):
                if not self.cfg.verifier_fail_open:
                    return False
        from engine.pulse.strategy import PulseDecision
        eid = proposal.event_id
        w = self._find_directional_window(eid)
        if w is None or self.ledger.has_position(eid):
            return False
        if not self._directional_series_allowed(w):
            return False
        now = time.time()
        _he_res = self._osmani_hourly_entry_check(w, now)
        if _he_res.get("decision") == "reject":
            return False
        _hc_ok, _hc_reason, _h_lean = self._hourly_chart_lean_entry_ok(
            w, proposal.side, now)
        if not _hc_ok:
            return False
        up_blk, up_blk_reason = self._directional_up_blocked(proposal.side)
        if up_blk:
            return False
        fill = float(verified.fill_price or 0)
        if fill <= 0 or fill >= 1:
            return False

        # Favorites profile B — min entry + offline cell Phase-2 (30d walk-forward).
        _fav_gate = None
        try:
            from engine.pulse.favorites_policy import evaluate_osmani_fill
            _fav_gate = evaluate_osmani_fill(
                side=str(proposal.side),
                ask=fill,
                window=w,
                now=now,
                cell_learning=getattr(self, "cell_learning", None),
                cell_phase2_enabled=bool(self.cfg.cell_learning_phase2_enabled),
            )
            if not _fav_gate.allow:
                return False
        except Exception:  # noqa: BLE001
            logger.exception("favorites policy gate failed; continuing")

        # DOWN overconfidence filter (FULL_REPORT loser pattern: ask_down - fair_p_up gap).
        try:
            from engine.pulse.execution_gate import down_ask_fair_gap_blocks
            _fair_gap = self._osmani_fair_p(w, now)
            _max_gap = float(os.getenv("PULSE_DOWN_MAX_ASK_FAIR_GAP", "0.12") or 0.12)
            if down_ask_fair_gap_blocks(
                    side=proposal.side, ask=fill, fair_p_up=_fair_gap, max_gap=_max_gap):
                return False
        except Exception:  # noqa: BLE001
            pass

        # Unified p_exec: Grok-MC + digital + mkt, self-tuned by context (same as legacy tick)
        p_win = float(proposal.outcome_prob)
        _pe_osmani = None
        try:
            _pf = self._price_feed_for(w)
            _s_now = _pf.current()
            _sigma = _pf.sigma_per_sec(now)
            _snap = _pf.open_snapshot(w.event_id)
            _fair = self._osmani_fair_p(w, now)
            _poly = float(w.up_book.mid) if (w.up_book and w.up_book.mid is not None) else None
            if (_s_now is not None and _sigma is not None and _snap is not None
                    and bool(getattr(self.cfg, "p_exec_enabled", True))):
                _asset = self._window_asset(w)
                _ws = int(getattr(w, "window_seconds", 900) or 900)
                _horizon = "1h" if _ws >= 3600 else ("15m" if _ws >= 600 else "5m")
                _sso = float(w.seconds_since_open(now))
                _pe_osmani = self._build_p_exec(
                    side=proposal.side, fair_used=_fair, poly_yes=_poly,
                    vwap=fill, s_now=float(_s_now), s_open=float(_snap.price),
                    sigma=float(_sigma), ttc=float(w.seconds_to_close(now)),
                    sso=_sso, asset=_asset, horizon=_horizon, lead_state="none")
                if _pe_osmani.get("p_exec") is not None:
                    p_win = float(_pe_osmani["p_exec"])
                if not _pe_osmani.get("allow", True):
                    return False
        except Exception:  # noqa: BLE001
            logger.debug("osmani p_exec skipped", exc_info=True)

        # ---- Autonomous bet size (half-Kelly × pre-trade readiness) ----
        readiness_scale = 1.0
        if _fav_gate is not None and float(_fav_gate.size_mult) != 1.0:
            readiness_scale *= float(_fav_gate.size_mult)
        pre_trade_snap = None
        if self.cfg.pre_trade_analysis_enabled and self.pre_trade_gate is not None:
            try:
                from engine.pulse.pre_trade_analysis import analyze_pre_trade
                sso = float(_he_res.get("seconds_since_open")
                            or w.seconds_since_open(now))
                ttc = float(w.seconds_to_close(now))
                tv_2h = (self._tv_2h_for_window(w, now)
                         if self.cfg.tv_2h_review_pretrade else None)
                from engine.pulse.tradingview import tv_symbol_for_window
                _tv_sym = tv_symbol_for_window(w, default_btc=self.cfg.tradingview_feature_symbol)
                _tv_per_tf = (self._tv_per_tf_views(
                    now, symbol=_tv_sym, tfs=self._tv_mtf_timeframes_for_window(w))
                    if self.tradingview is not None else None)
                analysis = analyze_pre_trade(
                    fair_p_up=(p_win if proposal.side == "up" else (1.0 - p_win)),
                    poly_yes=fill if proposal.side == "up" else (1.0 - fill),
                    proposed_side=proposal.side,
                    proposed_p_up=p_win if proposal.side == "up" else (1.0 - p_win),
                    ttc_s=ttc,
                    window_seconds=int(getattr(w, "window_seconds", 3600) or 3600),
                    seconds_since_open=sso,
                    up_ask=fill if proposal.side == "up" else None,
                    down_ask=fill if proposal.side == "down" else None,
                    min_edge=float(self.cfg.min_edge),
                    hourly_min_minutes=float(self.cfg.pre_trade_hourly_min_minutes),
                    tv_2h_review=tv_2h,
                    tv_per_tf_views=_tv_per_tf,
                )
                readiness_scale *= float(self.pre_trade_gate.size_scale(analysis))
                pre_trade_snap = {
                    "score": analysis.get("score"),
                    "recommendation": analysis.get("recommendation"),
                    "size_scale": readiness_scale,
                    "summary": analysis.get("summary"),
                }
            except Exception:  # noqa: BLE001 — sizing never blocks on analysis errors
                logger.exception("osmani pre-trade sizing analysis failed; scale=1.0")
                readiness_scale = 1.0

        from engine.pulse.hourly_entry_timing import is_hourly_window
        if (is_hourly_window(int(getattr(w, "window_seconds", 3600) or 3600))
                and bool(getattr(self.cfg, "tv_1h_chart_lean_size", True))
                and _h_lean):
            from engine.pulse.tv_15m_price_path import size_mult_for_lean
            readiness_scale *= float(size_mult_for_lean(side=proposal.side, lean=_h_lean))
        _rsi_ov = self._tv_rsi_overlay_for_window(w, now)
        if (bool(getattr(self.cfg, "tv_rsi_overlay_enabled", True))
                and bool(getattr(self.cfg, "tv_rsi_overlay_size", True))
                and _rsi_ov):
            from engine.pulse.tv_rsi_overlay import size_mult_for_rsi_overlay
            readiness_scale *= float(size_mult_for_rsi_overlay(
                side=proposal.side, overlay=_rsi_ov,
                aligned_mult=float(getattr(self.cfg, "tv_rsi_overlay_aligned_mult", 1.15)),
                opposed_mult=float(getattr(self.cfg, "tv_rsi_overlay_opposed_mult", 0.45))))

        # Binary Intel — universal 5m TV + binary math size mult (all lanes).
        _bi_osmani = None
        if getattr(self, "binary_intel", None) is not None and self.cfg.binary_intel_enabled:
            try:
                feed = self._price_feed_for(w)
                s_now = feed.current() if feed is not None else None
                s_open = getattr(_snap, "price", None) if _snap is not None else None
                sigma_use = None
                if feed is not None and hasattr(feed, "sigma_per_sec"):
                    sigma_use = feed.sigma_per_sec(now)
                _bi_osmani = self.binary_intel.analyze_pre_trade(
                    intake=self.tradingview,
                    window=w,
                    s_now=s_now,
                    s_open=s_open,
                    sigma_per_sec=sigma_use,
                    ttc_s=float(w.seconds_to_close(now)),
                    window_seconds=float(getattr(w, "window_seconds", 900) or 900),
                    poly_mid=fill if proposal.side == "up" else (1.0 - fill),
                    model_p_up=(p_win if proposal.side == "up" else (1.0 - p_win)),
                    proposed_side=proposal.side,
                    ask=fill,
                    now=float(now),
                    readiness_score=(pre_trade_snap or {}).get("score"),
                )
                if _bi_osmani:
                    if self.binary_intel.hard_block(_bi_osmani):
                        return False
                    readiness_scale *= float(self.binary_intel.size_mult(_bi_osmani))
            except Exception:  # noqa: BLE001
                logger.exception("osmani binary_intel failed; continuing")

        # SAWR — Empirical-Bayes side affinity size / soft-block (meta WR maximizer).
        if getattr(self, "sawr", None) is not None and bool(getattr(self.cfg, "sawr_enabled", True)):
            try:
                from engine.pulse.sawr_controller import lane_from_research
                _slug_s = str(getattr(w, "series_slug", "") or "").lower()
                _asset_s = "eth" if _slug_s.startswith("eth") else "btc"
                _lane_s = lane_from_research({
                    "series_slug": _slug_s,
                    "window_seconds": getattr(w, "window_seconds", 900),
                })
                _sawr_ev = self.sawr.evaluate_pre_trade(
                    side=str(proposal.side), ask=float(fill),
                    asset=_asset_s, lane=_lane_s)
                if _sawr_ev.get("soft_block"):
                    return False
                readiness_scale *= float(_sawr_ev.get("size_mult") or 1.0)
            except Exception:  # noqa: BLE001
                logger.exception("osmani sawr affinity failed; continuing")

        # CHRONOS — walk-forward cohort dry-run before bet size (invented pre-decision test).
        _chronos_cert = None
        if getattr(self, "chronos", None) is not None and bool(
                getattr(self.cfg, "chronos_enabled", True)):
            try:
                from engine.pulse.chronos_validator import asset_from_slug, lane_from_slug
                _slug_c = str(getattr(w, "series_slug", "") or "").lower()
                _ws_c = int(getattr(w, "window_seconds", 900) or 900)
                _chronos_cert = self.chronos.validate_trade(
                    positions=self.ledger.positions.values(),
                    asset=asset_from_slug(_slug_c),
                    lane=lane_from_slug(_slug_c, _ws_c),
                    side=str(proposal.side),
                    ask=float(fill),
                    now=float(now),
                    ttc_s=float(w.seconds_to_close(now)),
                    window_seconds=float(_ws_c),
                    model_p_win=float(p_win if proposal.side == "up" else (1.0 - p_win)),
                )
                if _chronos_cert.verdict == "block" and not _chronos_cert.exploration:
                    return False
                if _chronos_cert.verdict in ("probe", "cold_probe"):
                    readiness_scale = min(readiness_scale, 1.0)
                if float(_chronos_cert.size_cap_mult) < 1.0:
                    readiness_scale *= float(_chronos_cert.size_cap_mult)
            except Exception:  # noqa: BLE001
                logger.exception("osmani chronos dry-run failed; continuing")

        from engine.pulse.sizing import decide_trade_size
        size_decision = decide_trade_size(
            p_win=p_win,
            price=fill,
            ev_after_costs=verified.ev_after_slippage,
            bankroll_usd=self.cfg.sizing_bankroll_usd,
            hard_cap_usd=self.cfg.sizing_hard_cap_usd,
            daily_loss_cap_usd=self.cfg.sizing_daily_loss_cap_usd,
            daily_loss_so_far=self._daily_loss,
            base_size_usd=float(self.cfg.size_usd),
            min_size_usd=float(self.cfg.osmani_sizing_min_usd),
            readiness_scale=readiness_scale,
            sizing_enabled=(bool(self.cfg.sizing_enabled)
                            and bool(self.cfg.osmani_autonomous_sizing)),
        )
        trade_size = float(size_decision.get("size_usd") or 0.0)
        if trade_size <= 0:
            return False

        dir_cap = (float(self.cfg.starting_capital_usd)
                   * float(self.cfg.directional_max_bankroll_frac))
        if self._directional_open_exposure() + trade_size > dir_cap + 1e-6:
            return False
        if self.cfg.correlated_exposure_cap_usd > 0:
            corr = self._btc_correlated_exposure(proposal.side, time.time())
            if corr + trade_size > self.cfg.correlated_exposure_cap_usd + 1e-6:
                return False
        fair_up = float(p_win if proposal.side == "up" else (1.0 - p_win))
        dec = PulseDecision(
            trade=True,
            side=proposal.side,
            token_id=(w.up_token_id if proposal.side == "up" else w.down_token_id),
            price=fill,
            fair_p_up=fair_up,
            edge=float(verified.ev_after_slippage or 0),
            reason="osmani_lane_verified",
        )
        self.ledger.record_exec(True, "accepted")
        pos = self.ledger.open_position(
            w, dec, now,
            size_usd=trade_size,
            decision_id=proposal.proposal_id,
        )
        if pos is None:
            return False
        if pos.research is None:
            pos.research = {}
        _ttc = float(w.seconds_to_close(now))
        pos.research.update({
            "entry_mode": "osmani_lane",
            "ab_profile": (_fav_gate.ab_profile if _fav_gate is not None
                           else (os.getenv("PULSE_AB_PROFILE", "throughput") or "throughput")),
            "favorites_gate": (
                None if _fav_gate is None else {
                    "reason": _fav_gate.reason,
                    "size_mult": _fav_gate.size_mult,
                    "cell_verdict": _fav_gate.cell_verdict,
                    "cell_key": _fav_gate.cell_key,
                }),
            "ev_after_cost": verified.ev_after_slippage,
            "gate_decision": "osmani_verified",
            "series_slug": getattr(w, "series_slug", ""),
            "market_series": getattr(w, "series_label", ""),
            "window_seconds": int(getattr(w, "window_seconds", 3600) or 3600),
            "entry_ttc_s": _ttc,
            "seconds_since_open_at_entry": _he_res.get("seconds_since_open"),
            "hourly_entry_bucket": _he_res.get("bucket"),
            "hourly_gate_decision": _he_res.get("decision"),
            "tv_1h_chart_lean": _h_lean,
            "tv_1h_trade_lean": (_h_lean or {}).get("trade_lean"),
            "tv_1h_chart_alignment": (_h_lean or {}).get("alignment"),
            "tv_1h_short_pattern": (_h_lean or {}).get("short_pattern"),
            "tv_1h_regime_pattern": (_h_lean or {}).get("regime_pattern"),
            "tv_rsi_overlay": _rsi_ov,
            "tv_rsi_overlay_lean": (_rsi_ov or {}).get("lean") if _rsi_ov else None,
            "sizing": {
                "decision": size_decision.get("decision"),
                "size_usd": trade_size,
                "base_size_usd": float(self.cfg.size_usd),
                "kelly_fraction": size_decision.get("kelly_fraction"),
                "half_kelly": size_decision.get("half_kelly"),
                "readiness_scale": size_decision.get("readiness_scale"),
                "hard_cap_usd": size_decision.get("hard_cap_usd"),
                "autonomous": size_decision.get("autonomous"),
            },
            "pre_trade": pre_trade_snap,
        })
        if _rsi_ov:
            _rl = str((_rsi_ov or {}).get("lean") or "").lower()
            if _rl in ("up", "down"):
                pos.research["tv_rsi_overlay_aligned"] = (_rl == str(proposal.side).lower())
        if _bi_osmani:
            tags = (_bi_osmani.get("research_tags") or {})
            pos.research["binary_intel_score"] = _bi_osmani.get("composite_score")
            pos.research["binary_intel_intelligence"] = _bi_osmani.get("intelligence_score")
            pos.research["binary_intel_recommendation"] = _bi_osmani.get("recommendation")
            pos.research["binary_intel_size_mult"] = _bi_osmani.get("size_mult")
            pos.research["binary_intel_z"] = tags.get("binary_intel_z")
            pos.research["binary_intel_rsi_lean"] = tags.get("binary_intel_rsi_lean")
            pos.research["binary_intel_rsi_decision"] = tags.get("binary_intel_rsi_decision")
            pos.research["tv_cross_asset_rsi"] = tags.get("tv_cross_asset_rsi")
            if tags.get("tv_rsi_overlay_aligned") is not None:
                pos.research["tv_rsi_overlay_aligned"] = tags.get("tv_rsi_overlay_aligned")
            if tags.get("binary_intel_rsi_lean") and not pos.research.get("tv_rsi_overlay_lean"):
                pos.research["tv_rsi_overlay_lean"] = tags.get("binary_intel_rsi_lean")
        if _pe_osmani:
            pos.research["p_exec"] = _pe_osmani.get("p_exec")
            pos.research["p_blend"] = _pe_osmani.get("p_blend")
            pos.research["p_mc"] = _pe_osmani.get("p_mc")
            pos.research["p_mkt"] = _pe_osmani.get("p_mkt")
            pos.research["p_digital_side"] = _pe_osmani.get("p_digital_side")
            pos.research["p_exec_context"] = _pe_osmani.get("context_key")
            pos.research["p_exec_weights"] = _pe_osmani.get("weights")
            pos.research["dir_mc"] = {
                k: (_pe_osmani.get("mc") or {}).get(k)
                for k in ("p_mc", "p_mc_adj", "p_digital", "p_crash", "se", "available")
            }
            pos.research["outcome_prob"] = p_win
            pos.research["outcome_prob_source"] = "p_exec"
        grok_dec = (self.grok_decider.get(eid) if self.grok_decider is not None else None)
        if grok_dec:
            pos.research["grok_snapshot"] = {
                "action": grok_dec.get("action"),
                "p_up": grok_dec.get("p_up"),
                "confidence": grok_dec.get("confidence"),
            }
        if self.verifier is not None:
            vv_snap = self.verifier.get(eid)
            if vv_snap and not vv_snap.get("pending"):
                pos.research["verifier_snapshot"] = {
                    "approved": bool(vv_snap.get("approve")),
                    "reason": str(vv_snap.get("reason") or "")[:120],
                }
        return True

    def _loops_report(self) -> dict:
        """Loop registry with live verified stop-condition strings (refreshed each tick)."""
        rep = self.loops.report()
        loops = rep.get("loops") or {}
        if "directional" in loops:
            loops["directional"]["stop_condition"] = self.stop_monitor.verified_stop_line(
                "directional")
        return rep

    def _register_loops(self) -> None:
        """Formalize the sub-loops for uniform observability (#3)."""
        r = self.loops
        r.register("heartbeat", role="automation", trigger="tick",
                   interval_s=self.cfg.tick_seconds, skill="AGENTS.md",
                   stop_condition="process running")
        r.register("directional", role="strategy", trigger="per_window",
                   skill="digital model + allowlist",
                   stop_condition=self.stop_monitor.verified_stop_line("directional"),
                   status_fn=lambda: {
                       "enabled": self.cfg.directional_enabled,
                       "halted": self.stop_monitor.is_halted("directional"),
                       "authority": ("osmani_lanes" if self._directional_trade_authority_osmani()
                                     else "legacy_tick"),
                       "legacy_tick": bool(self.cfg.directional_legacy_tick),
                   })
        r.register("data_ingestion", role="data", trigger="tick", skill="price/book/CEX/RTDS",
                   status_fn=lambda: {"enabled": True})
        if self.tradingview is not None:
            r.register("tradingview", role="context", trigger="webhook",
                       skill="TV alerts + observe-only context features",
                       stop_condition="observe-only context feed",
                       status_fn=lambda: {
                           "enabled": True,
                           "received": self.tradingview.received,
                           "valid": self.tradingview.valid,
                           "rejected": self.tradingview.rejected,
                       })
        r.register("signal_generation", role="signal", trigger="per_window",
                   skill="research/factors/markov/edge_model",
                   status_fn=(lambda: self._grok_decider_report()) if self.grok_decider else None)
        r.register("verifier", role="verify(maker-checker)", trigger="per_decision",
                   skill="independent Claude verdict", verifier="claude",
                   stop_condition="approve/veto verdict",
                   status_fn=(lambda: self.verifier.report()) if self.verifier else None)
        r.register("execution", role="execute", trigger="per_decision",
                   skill="execution-quality gate (authoritative)", stop_condition="fill or reject")
        r.register("risk_monitor", role="risk", trigger="per_settlement",
                   skill="breaker + reconciliation",
                   status_fn=(lambda: self.grok_decider.breaker_status()) if self.grok_decider else None)
        r.register("news", role="context", trigger="interval",
                   interval_s=self.cfg.grok_news_refresh_s,
                   status_fn=(lambda: self.grok_news.report()) if self.grok_news else None)
        r.register("research_meta", role="research(/goal)", trigger="interval",
                   interval_s=self.cfg.research_interval_s, verifier="claude",
                   stop_condition="verifiable metric improvement",
                   status_fn=(lambda: self.research_loop.report()) if self.research_loop else None)
        r.register("loop_synthesis", role="loop_engine(WS5)", trigger="per_light_report",
                   skill="loop_synthesis.py",
                   stop_condition="evidence-gated next experiment",
                   status_fn=lambda: getattr(self, "_loop_synthesis_cache", {}) or {})
        if self.osmani_loop is not None:
            ol = self.osmani_loop
            r.register(
                "osmani_discovery",
                role="discovery_lane",
                trigger="timer",
                interval_s=self.cfg.osmani_discovery_interval_s,
                skill="polymarket_asset_triage",
                verifier="TradeEvaluator",
                stop_condition="circuit_breaker",
                status_fn=ol.discovery.report,
            )
            r.register(
                "osmani_execution",
                role="execution_lane",
                trigger="queue",
                skill="worktree_isolated_placement",
                verifier="independent_api_book_check",
                stop_condition="circuit_breaker",
                status_fn=ol.execution.report,
            )
            r.register(
                "osmani_ledger",
                role="ledger_lane",
                trigger="queue",
                skill="single_writer_persist",
                stop_condition="disk_write_ok",
                status_fn=ol.ledger.report,
            )
        r.register("lessons", role="memory", trigger="per_settlement",
                   skill="LESSONS.md + MEMORY.md",
                   status_fn=lambda: {"calls": len(self.lessons.lessons)})

    def _capital_status(self) -> dict:
        """On-hand paper capital = starting capital + realized PnL, with open exposure (stake at risk
        in open positions). Display-only; PAPER ONLY (no real funds)."""
        ls = self.ledger.stats()
        start = float(self.cfg.starting_capital_usd)
        realized = float(ls.get("realized_pnl_usd") or 0.0)
        open_exposure = 0.0
        for pos in self.ledger.positions.values():
            if pos.status == "open":
                open_exposure += float(getattr(pos, "size_usd", 0.0) or 0.0)
        on_hand = start + realized
        dir_cap = round(start * float(self.cfg.directional_max_bankroll_frac), 2)
        total_realized = realized
        primary = "directional" if total_realized > 0 else self.cfg.primary_edge_source
        return {"paper_only": True, "starting_capital_usd": round(start, 2),
                "realized_pnl_usd": round(realized, 2),
                "on_hand_capital_usd": round(on_hand, 2),
                "return_pct": (round(realized / start * 100, 2) if start else None),
                "total_realized_pnl_usd": round(total_realized, 2),
                "total_on_hand_usd": round(start + total_realized, 2),
                "total_return_pct": (round(total_realized / start * 100, 2) if start else None),
                "open_exposure_usd": round(open_exposure, 2),
                "directional_bankroll_cap_usd": dir_cap,
                "directional_cap_remaining_usd": round(max(0.0, dir_cap - open_exposure), 2),
                "primary_edge_source": primary,
                "open_positions": ls.get("open_positions")}

    def _grok_decider_report(self) -> dict:
        """Grok Decision Engine status (off/shadow/follow): decisions, direction accuracy, Brier,
        latency, abstains, per-action breakdown. PAPER ONLY; shadow does not trade."""
        if self.grok_decider is None:
            return {"enabled": False, "mode": self.cfg.grok_decider_mode, "paper_only": True,
                    "affects_trading": False}
        rep = self.grok_decider.report()
        rep["pending_grades"] = len(self._grok_pending)
        rep["use_search"] = bool(self.cfg.grok_decider_use_search)
        rep["adaptive_enabled"] = bool(self.cfg.grok_decider_adaptive)
        rep["adaptive_policy_counts"] = dict(self._grok_policy_counts)
        rep["mispricing_gate"] = {
            "enabled": bool(self.cfg.mispricing_gate_enabled),
            "edge_ttc_gate_enabled": bool(self.cfg.edge_ttc_gate_enabled),
            "follow_on_abstain": bool(self.cfg.mispricing_follow_on_abstain),
            "follow_size_fraction": self.cfg.mispricing_follow_size_fraction,
            "ttc_window_s": [self.cfg.mispricing_ttc_min_s, self.cfg.mispricing_ttc_max_s],
            "min_executable_margin": self.cfg.mispricing_min_executable_margin,
            "reject_counts": dict(self._mispricing_gate_counts),
        }
        rep["explore_rate"] = self.cfg.grok_decider_explore_rate
        rep["news_digest"] = (self.grok_news.report() if self.grok_news is not None
                              else {"enabled": False})
        return rep

    def _grok_intel_report(self) -> dict:
        """Observe-only Grok signal-intelligence status (A analyst + B predictor + budget)."""
        return {
            "observe_only": True, "affects_trading": False, "off_hot_path": True,
            "budget": (self.grok_budget.status() if self.grok_budget is not None
                       else {"enabled": False}),
            "analyst_A": (self.grok_analyst.report() if self.grok_analyst is not None
                          else {"enabled": False}),
            "predictor_B": (self.grok_predictor.report() if self.grok_predictor is not None
                            else {"enabled": False}),
            "note": ("A analyzes signal-learning performance; B predicts P(up) per signal and is "
                     "graded vs realized moves. Both observe-only — never place/size/bypass a "
                     "trade; the execution gate remains the sole trade authority."),
        }

    def _update_prism_information(self, now: float, windows: list) -> None:
        """PRISM Phase 2 (observe-only): refresh information completeness I(t) from the latest TV
        ladder + anchor freshness, and the hour-timing FSM from the nearest open directional window.

        PAPER ONLY — this never blocks or allows a trade; PRISM is wired into the decision path only
        in the final integration phase. Failures are swallowed by the caller.
        """
        from engine.pulse.prism.information import ingest_tv_latest
        sym = self.cfg.tradingview_feature_symbol or "BTCUSD"
        if self.tradingview is not None:
            lbt = (self._tradingview_report() or {}).get("tradingview_latest_by_timeframe") or {}
            ingest_tv_latest(lbt, sym, now, tracker=self.prism_info)
        if self.price.is_fresh(30.0, now):
            self.prism_info.observe("chainlink_anchor", now, now)
        sso = None
        for w in (windows or []):
            if not getattr(w, "directional_lane", False):
                continue
            try:
                sso = float(w.seconds_since_open(now))
            except Exception:  # noqa: BLE001
                sso = None
            break
        self._prism_info_report = self.prism_info.to_report(now, sso)

    _TIER_TV_TFS = ("3", "4", "5", "15", "30", "45", "60", "240", "1440")

    def _cell_learning_tv_ladder(self, w, sym: str) -> dict:
        """Latest TV snapshots for cell learning / tier ladder on this window's asset."""
        lbt = ((self._tradingview_report() or {}).get("tradingview_latest_by_timeframe")) or {}
        tv_by_tf = {}
        for tf in self._tier_tv_tfs_for_window(w):
            snapv = lbt.get("%s@%s" % (sym, tf))
            if snapv:
                tv_by_tf[tf] = snapv
        return tv_by_tf

    def _tier_apply_cell_phase2(self, w, mc, td, now):
        """Nudge tier posterior + size from mature Wilson cell verdict (directional lane only)."""
        from engine.pulse.directional_cell_learning import apply_phase2_to_tier_decision
        from engine.pulse.tradingview import tv_symbol_for_window
        sym = tv_symbol_for_window(w) or "BTCUSD"
        tv_by_tf = self._cell_learning_tv_ladder(w, sym)
        ask = None
        if td.side == "up" and w.up_book is not None:
            ask = w.up_book.best_ask
        elif td.side == "down" and w.down_book is not None:
            ask = w.down_book.best_ask
        info_I = float((self._prism_info_report or {}).get("I") or 0.0)
        ck = self.cell_learning.key_from_context(
            series_slug=getattr(w, "series_slug", ""),
            series_label=getattr(w, "series_label", mc.series_label),
            sso=float(w.seconds_since_open(now)), regime=td.regime.value,
            tv_by_tf=tv_by_tf, side=td.side, ask=ask, information_I=info_I)
        adj = self.cell_learning.phase2_adjustment(ck)
        return apply_phase2_to_tier_decision(
            td, adj,
            ask_up=(w.up_book.best_ask if w.up_book is not None else None),
            ask_down=(w.down_book.best_ask if w.down_book is not None else None),
            down_only=bool(self.cfg.directional_down_only))

    def _tier_evaluate(self, w, mc, snap, s_now, sigma, ov_vol_mult, ttc, now, cand_state,
                       ov_blackout):
        """Build the TV ladder for this window's asset and run the Directional Tier Engine.
        Returns a TierDecision (or None on any error -> candidate rejected as tier_no_decision)."""
        if self.tier_engine is None or s_now is None or snap is None or not sigma:
            return None
        from engine.pulse.tradingview import tv_symbol_for_window
        sym = tv_symbol_for_window(w) or "BTCUSD"
        lbt = ((self._tradingview_report() or {}).get("tradingview_latest_by_timeframe")) or {}
        tv_by_tf = self._cell_learning_tv_ladder(w, sym)
        up_book, down_book = w.up_book, w.down_book
        jump_risk = bool(ov_blackout) or (float(ov_vol_mult or 1.0) >= 1.5)
        open_corr = 0.0
        try:
            if self.cfg.correlated_exposure_cap_usd > 0:
                _side_guess = "down" if self.cfg.directional_down_only else "up"
                corr = self._btc_correlated_exposure(_side_guess, now)
                open_corr = min(1.0, corr / max(1.0, self.cfg.correlated_exposure_cap_usd))
        except Exception:  # noqa: BLE001
            open_corr = 0.0
        ws = float(getattr(w, "window_seconds", 3600) or 3600)
        overlay = None
        down_only = bool(self.cfg.directional_down_only)
        if (self._is_15m_window(w) and getattr(self, "lane_15m_learner", None) is not None
                and self.lane_15m_learner.cfg.enabled):
            pol = self.lane_15m_learner.policy
            overlay = {
                "min_sso": float(pol.min_sso),
                "sweet_min": float(pol.sweet_min),
                "sweet_max": float(pol.sweet_max),
                "strike_edge_min": float(pol.strike_edge_min),
                "harvest_edge_min": float(pol.harvest_edge_min),
                "probe_enabled": bool(pol.probe_enabled),
            }
            if pol.side_mode == "down_only":
                down_only = True
            elif pol.side_mode == "up_only":
                down_only = False  # force up via post-filter; tier still picks best edge
        return self.tier_engine.evaluate(
            window_key=mc.decision_id, sso=float(w.seconds_since_open(now)), ttc_s=float(ttc),
            s_now=float(s_now), s_open=float(self._directional_fair_anchor(w, snap) or snap.price),
            sigma_per_sec=float(sigma) * float(ov_vol_mult or 1.0),
            ask_up=(up_book.best_ask if up_book is not None else None),
            ask_down=(down_book.best_ask if down_book is not None else None),
            tv_by_tf=tv_by_tf, now=float(now),
            ask_depth_up=(up_book.ask_depth_usd if up_book is not None else None),
            ask_depth_down=(down_book.ask_depth_usd if down_book is not None else None),
            open_corr=open_corr, jump_risk=jump_risk,
            down_only=down_only,
            window_seconds=ws,
            overlay=overlay)

    def _cell_learning_log_tier(self, w, mc, td, now, *, traded: bool) -> None:
        """Log tier eval into the cell table (every tick on directional lane windows)."""
        if self.cell_learning is None or td is None:
            return
        if not getattr(w, "directional_lane", False):
            return
        from engine.pulse.tradingview import tv_symbol_for_window
        wslug = getattr(w, "series_slug", "")
        sym = tv_symbol_for_window(w) or "BTCUSD"
        tv_by_tf = self._cell_learning_tv_ladder(w, sym)
        ask = None
        if td.side == "up" and w.up_book is not None:
            ask = w.up_book.best_ask
        elif td.side == "down" and w.down_book is not None:
            ask = w.down_book.best_ask
        info_I = float((self._prism_info_report or {}).get("I") or 0.0)
        ck = self.cell_learning.key_from_context(
            series_slug=wslug, series_label=getattr(w, "series_label", mc.series_label),
            sso=float(w.seconds_since_open(now)), regime=td.regime.value,
            tv_by_tf=tv_by_tf, side=td.side, ask=ask, information_I=info_I)
        self.cell_learning.log_eval(
            mc.decision_id, ck, tier=td.tier.value, side=td.side, edge=float(td.edge),
            p_up=float(td.p_up), series_slug=wslug, traded=traded)
        self._last_cell_key = ck
        self._last_cell_tier = td

    def _tradingview_report(self) -> dict:
        """Observe-only TradingView intake counters + latest signal + signal-vs-5min-outcome edge
        measurement (report-only)."""
        if self.tradingview is None:
            rep = {"enabled": False, "tradingview_observe_only": True,
                   "tradingview_alerts_received": 0, "tradingview_alerts_valid": 0,
                   "tradingview_alerts_rejected": 0, "tradingview_reject_reasons": {},
                   "tradingview_latest_signal": None}
        else:
            rep = self.tradingview.report()
        # always surface the webhook listener status (req: listener status in the light report)
        rep["webhook"] = (self.webhook.status() if self.webhook is not None
                          else {"listening": False, "observe_only": True,
                                "reason": ("no_secret_configured" if self.tradingview is None
                                           else "listener_not_started")})
        rep["edge_vs_5min_outcome"] = self._tv_edge.report()
        rep["rsi_trend"] = self._rsi_model.report()
        rep["rsi_trend"]["forward_horizon_s"] = self.cfg.tradingview_signal_horizon_s
        rep["rsi_trend"]["pending_forward_evals"] = len(self._tv_pending)
        rep["rsi_trend"]["learns_from"] = "all_signals_forward_return"
        rep["signal_learning"] = self._tv_learner.report(
            promotion_allowed=self.cfg.tradingview_promotion_allowed,
            min_samples=self.cfg.tradingview_promotion_min_samples,
            min_win_rate=self.cfg.tradingview_promotion_min_win_rate)
        rep["strong_fade"] = {
            "enabled": bool(self.cfg.tv_strong_fade_enabled),
            "scope": "1h_directional_only",
            "rule": "reject entry when fresh signal_level matches {SIDE}_STRONG",
            "reject_counts": dict(self._tv_strong_fade_counts),
        }
        rep["signal_gate"] = {
            "enabled": bool(self.cfg.tradingview_signal_gate_enabled),
            "active": bool(self.cfg.tradingview_signal_gate_enabled and self.tradingview is not None),
            "mode": "directional_indication_restrict_only",
            "requires_fresh_aligned_signal": True, "can_force_trade": False,
            "execution_gate_still_authoritative": True,
            "max_signal_age_s": self.cfg.tradingview_signal_max_feature_age_s,
            "min_signal_strength": (self.cfg.tradingview_min_signal_strength
                                    if self.cfg.tradingview_min_signal_strength > 0 else None),
            "note": ("when active, a paper trade is taken only if a fresh TradingView signal agrees "
                     "with the side; it can only PREVENT trades, never force or bypass them.")}
        rep["context_gate"] = self.tv_context_gate.report()
        rep["down_bias_gate"] = self.tv_down_bias_gate.report()
        rep["mtf_gate"] = self.tv_mtf_gate.report()
        rep["confidence_tier"] = self._tv_confidence_tier_report()
        rep["tv_2h_review"] = self._tv_2h_review_report()
        # Last-50 FIFO → OHLC price-path trend for Grok / 15m lane (observe-only).
        if self.tradingview is not None:
            try:
                from engine.pulse.tv_15m_price_path import tv_15m_price_path_snapshot
                _cap = max(1, int(self.cfg.tradingview_alert_history_per_symbol or 50))
                _hist = self.tradingview.alert_history_snapshot(
                    focus_symbol=self.cfg.tradingview_feature_symbol)
                rep["tradingview_alert_history_per_symbol"] = _cap
                rep["tradingview_alert_history_counts"] = {
                    sym: len(rows) for sym, rows in (_hist.get("by_symbol") or {}).items()
                }
                rep["tradingview_15m_price_path"] = tv_15m_price_path_snapshot(
                    history=_hist,
                    focus_symbol=self.cfg.tradingview_feature_symbol,
                    max_points=_cap,
                    short_n=max(6, min(int(getattr(self.cfg, "tv_15m_short_path_n", 8) or 8), _cap)),
                )
                rep["tradingview_rsi_band"] = self._tv_rsi_band_for_window(
                    None, self.last_tick_ts or time.time())
                rep["tradingview_rsi_divergence"] = self._tv_rsi_divergence_for_window(
                    None, self.last_tick_ts or time.time())
            except Exception:  # noqa: BLE001
                rep["tradingview_15m_price_path"] = {"enabled": False, "error": "build_failed"}
                rep["tradingview_rsi_band"] = {"enabled": False, "error": "build_failed"}
                rep["tradingview_rsi_divergence"] = {"enabled": False, "error": "build_failed"}
        else:
            rep["tradingview_alert_history_per_symbol"] = int(
                self.cfg.tradingview_alert_history_per_symbol or 50)
            rep["tradingview_15m_price_path"] = {"enabled": False}
        return rep

    def _tv_confidence_tier_report(self) -> dict:
        return {
            "enabled": bool(self.cfg.tv_confidence_tier_enabled),
            "observe_only": True,
            "affects_trading": bool(self.cfg.tv_confidence_tier_enabled),
            "can_force_trade": False,
            "can_block_trade": False,
            "mode": "param_modulation_restrict_only",
            "only_15m": bool(self.cfg.tv_tier_15m_only),
            "require_sweet_spot": bool(self.cfg.tv_tier_require_sweet_spot),
            "tier_counts": dict(self._tv_tier_counts),
            "deltas": {
                "tier_a_min_edge": self.cfg.tv_tier_a_min_edge_delta,
                "tier_a_max_price": self.cfg.tv_tier_a_max_price_delta,
                "tier_c_min_edge": self.cfg.tv_tier_c_min_edge_delta,
                "tier_c_max_price": self.cfg.tv_tier_c_max_price_delta,
            },
            "aligned_strength_min": self.cfg.tv_tier_aligned_strength_min,
            "note": ("At 15m TTC sweet spot, TV MTF regime adjusts min_edge/max_price only. "
                     "TV trade gates remain off per operator lock."),
        }

    def _tier_report(self) -> dict:
        """REPORT-ONLY tier table across bucket dimensions (no trade/veto authority)."""
        from engine.pulse.tiers import tier_report
        dims = {}
        if self.factors is not None:
            dims["edge_quality"] = self.factors.report().get("pnl_by_edge_quality_bucket", {})
        if self.research is not None:
            rr = self.research.report()
            dims["regime"] = rr.get("pnl_by_regime", {})
            dims["zscore_bucket"] = rr.get("pnl_by_zscore_bucket", {})
            dims["ttc_bucket"] = rr.get("pnl_by_ttc_bucket", {})
        reconciled = bool(self.reconciler.report().get("reconciled"))
        return tier_report(dims, reconciled=reconciled, safety_ok=reconciled)

    def status(self) -> dict:
        from engine.pulse.reporting import (ledger_stats_by_market_series,
                                            ledger_stats_by_entry_price,
                                            ledger_wr_ev_books)
        return {
            "schema": "btc_pulse/1.1", "paper_only": True, "live_trading_enabled": False,
            "ts": self.last_tick_ts, "ticks": self.ticks,
            "config": {"tick_seconds": self.cfg.tick_seconds, "size_usd": self.cfg.size_usd,
                       "strategy_version": DIRECTIONAL_LEARNING_VERSION,
                       "min_edge": self.cfg.min_edge, "edge_buffer": self.cfg.edge_buffer,
                       "min_depth_usd": self.cfg.min_depth_usd, "max_price": self.cfg.max_price,
                       "exec_min_ev_after_fees": self.cfg.exec_min_ev_after_slippage,
                       "exec_max_spread": self.cfg.exec_max_spread,
                       "require_proven_winning_bucket": self.cfg.directional_require_winning_bucket,
                       "min_reward_risk": self.cfg.min_reward_risk,
                       "grok_decider_mode": self.cfg.grok_decider_mode},
            "price": self.price.status(),
            "eth_price": (self._eth_price.status() if self._eth_price is not None
                           else {"enabled": False}),
            "hourly_resolution_prices": {
                "btc": (self._btc_hourly_price.status()
                        if self._btc_hourly_price is not None else {"enabled": False}),
                "eth": (self._eth_hourly_price.status()
                        if self._eth_hourly_price is not None else {"enabled": False}),
            },
            "capital": self._capital_status(),
            "ledger": self.ledger.stats(),
            "decision_lifecycle": self.reconciler.report(),
            "reconciliation": self._global_reconciliation(),
            "signal_engine": (self.signals.report() if self.signals is not None
                              else {"enabled": False}),
            "factor_model": (self.factors.report() if self.factors is not None
                             else {"enabled": False}),
            "markov_regime": (self.markov.report() if self.markov is not None
                              else {"enabled": False}),
            "edge_model": (self.edge_model.report(affects_trading=self._learning_report()["active"])
                           if self.edge_model is not None else {"enabled": False}),
            "learning": self._learning_report(),
            "tier_table": self._tier_report(),
            "meta_learning": self._meta_learning_status(),
            "promotion_ladder": self.promotion.report(),
            "readiness": self.readiness(),
            "sizing": {"enabled": self.cfg.sizing_enabled, "paper_only": True,
                       "hard_cap_usd": self.cfg.sizing_hard_cap_usd,
                       "daily_loss_cap_usd": self.cfg.sizing_daily_loss_cap_usd,
                       "daily_loss_so_far": round(self._daily_loss, 4),
                       "bankroll_usd": self.cfg.sizing_bankroll_usd,
                       "no_martingale": True, "actual_size_usd": self.cfg.size_usd,
                       "osmani_autonomous": bool(self.cfg.osmani_autonomous_sizing),
                       "osmani_min_usd": float(self.cfg.osmani_sizing_min_usd),
                       "base_size_usd": float(self.cfg.size_usd),
                       "note": ("Osmani decides size via half-Kelly × pre-trade readiness "
                                "when enabled; clamped to [min, hard_cap].")},
            "execution_gate": self.ledger.exec_gate_stats(),
            "research_features": (self.research.report() if self.research is not None
                                  else {"enabled": False}),
            "calibration": self.calib.to_dict(),
            "oracle": {
                "oracle_feed_type": self.oracle_feed_type,
                "oracle_symbol": self.cfg.oracle_symbol,
                "fast_feed_symbols": list(self.cfg.fast_feeds),
                "open_snapshot_source": "rtds_chainlink",
                "close_snapshot_source": "rtds_chainlink",
                "settlement_source_priority": ["polymarket_resolution"],
                "settlement_sources_used": self.ledger.stats().get("settle_sources"),
                "proxy_official_reconciliation":
                    self.ledger.stats().get("proxy_official_reconciliation"),
                "proxy_max_close_lag_s": self.cfg.proxy_max_close_lag_s,
                "rtds": (self.rtds.status() if self.rtds is not None else {"enabled": False}),
                "eth_rtds": (self._eth_rtds.status()
                             if self._eth_rtds is not None else {"enabled": False}),
                "lead_features": self.leads.features(),
            },
            "grok_overlay": (self.overlay.status() if self.overlay is not None
                             else {"enabled": False}),
            "grok_signal_intel": self._grok_intel_report(),
            "grok_decider": self._grok_decider_report(),
            "llm_council": (self.llm_council.report() if self.llm_council is not None
                            else {"enabled": False}),
            "claude_decider": (self.claude_decider.report() if self.claude_decider is not None
                               else {"enabled": False}),
            "monte_carlo": {
                "enabled": bool(getattr(self.cfg, "dir_mc_enabled", True)),
                "paths": int(getattr(self.cfg, "dir_mc_paths", 8000) or 8000),
                "scenario": (self.mc_scenario.report() if getattr(self, "mc_scenario", None)
                             is not None else {"enabled": False}),
            },
            "p_exec": (self.p_exec_tune.report() if getattr(self, "p_exec_tune", None)
                       is not None else {}),
            "verifier": (self.verifier.report() if self.verifier is not None else {"enabled": False}),
            "research_loop": (self.research_loop.report() if self.research_loop is not None
                              else {"enabled": False}),
            "lessons": self.lessons.report(),
            "loops": self._loops_report(),
            "stop_conditions": self.stop_monitor.report(),
            "edge_signal": self._edge_signal_report(),
            "cex_lead_edge": (self.cex_lead.report() if self.cex_lead is not None
                              else {"enabled": False}),
            "osmani_loop": (self.osmani_loop.report()
                            if self.osmani_loop is not None
                            else {"enabled": False}),
            "ab_experiment": self._ab_experiment_status(),
            "prism_information": getattr(self, "_prism_info_report", {"enabled": True}),
            "prism_stopping": (self.prism_stopping.to_report()
                               if getattr(self, "prism_stopping", None) is not None
                               else {"enabled": False}),
            "prism_ensemble": getattr(self, "_prism_ensemble_report", {"enabled": False}),
            "prism_thompson": (self.prism_thompson.report()
                               if getattr(self, "prism_thompson", None) is not None
                               else {"enabled": False}),
            "prism_agents": getattr(self, "_prism_agent_report", {"enabled": False}),
            "tier_engine": (self.tier_engine.to_report()
                            if getattr(self, "tier_engine", None) is not None
                            else {"enabled": False}),
            "cell_learning": (self.cell_learning.report(
                phase2_enabled=bool(self.cfg.cell_learning_phase2_enabled))
                              if getattr(self, "cell_learning", None) is not None
                              else {"enabled": False, "observe_only": True}),
            "prism": {
                "enabled": bool(self.cfg.prism_enabled),
                "trade_authority": bool(self.cfg.prism_agent_gate_enabled),
                "stopping_gate": bool(self.cfg.prism_stopping_enabled),
                "thompson_gate": bool(self.cfg.prism_thompson_gate_enabled),
                "cross_asset": bool(self.cfg.prism_cross_asset_enabled),
                "ensemble": getattr(self, "_prism_ensemble_report", {"enabled": False}),
                "agents": getattr(self, "_prism_agent_report", {"enabled": False}),
                "information": getattr(self, "_prism_info_report", {"enabled": False}),
            },
            "clob_feed": (
                self.clob_feed.latency_report() if getattr(self, "clob_feed", None) else {}),
            "walk_forward": self._walk_forward_status(),
            "series_architecture": {
                "design": "5m_brain_15m_hands",
                "scan_slugs": list(self.cfg.pulse_series_slugs),
                "directional_slugs": list(self.cfg.directional_series_slugs),
                "directional_hourly": (
                    self._directional_hourly_feed.report()
                    if getattr(self, "_directional_hourly_feed", None) is not None
                    else {"enabled": False}),
                "directional_15m": (
                    self._directional_15m_feed.report()
                    if getattr(self, "_directional_15m_feed", None) is not None
                    else {"enabled": False}),
            },
            "profit_discovery": self._profit_discovery_status(),
            "five_x_improvement": self._profit_discovery_status(),
            "directional_risk": {
                "directional_enabled": bool(self.cfg.directional_enabled),
                "strategy_mode": ("directional" if self.cfg.directional_enabled else "disabled"),
                "max_bankroll_frac": self.cfg.directional_max_bankroll_frac,
                "bankroll_cap_usd": round(
                    float(self.cfg.starting_capital_usd)
                    * float(self.cfg.directional_max_bankroll_frac), 2),
                "open_exposure_usd": round(self._directional_open_exposure(), 2),
                "correlated_exposure_cap_usd": float(self.cfg.correlated_exposure_cap_usd),
                "correlated_up_exposure_usd": self._btc_correlated_exposure("up"),
                "correlated_down_exposure_usd": self._btc_correlated_exposure("down"),
                "block_up_until_promoted": bool(self.cfg.directional_block_up_until_promoted),
                "directional_down_only": bool(self.cfg.directional_down_only),
                "directional_series_slugs": list(self.cfg.directional_series_slugs),
                "up_promoted": self._up_direction_promoted(),
            },
            "directional_allowlist": {
                "enabled": bool(self.cfg.directional_require_winning_bucket),
                "explore_rate": self.cfg.directional_explore_rate,
                "explored": self._allowlist_explored, "blocked": self._allowlist_blocked},
            "by_market_series": ledger_stats_by_market_series(self.ledger.positions),
            "directional_by_entry_price": ledger_stats_by_entry_price(self.ledger.positions),
            "high_wr_mode": {
                "enabled": str(os.getenv("PULSE_HIGH_WR_MODE", "0")).strip().lower()
                           in ("1", "true", "yes", "on"),
                "min_entry_price": float(self.cfg.min_entry_price),
                "min_edge": float(self.cfg.min_edge),
                "directional_require_winning": bool(
                    self.cfg.directional_require_winning_bucket),
                "directional_explore_rate": float(self.cfg.directional_explore_rate),
                "hourly_min_sso": float(self.cfg.hourly_min_seconds_since_open),
                "late_window_entry": bool(self.cfg.late_window_entry_enabled),
                "books": ledger_wr_ev_books(
                    self.ledger.positions,
                    wr_entry_floor=float(self.cfg.min_entry_price)),
                "note": ("Hourly throughput mode: loose floors for ~1 fill/hour/symbol; "
                         "GateAutoTuner tightens/loosens from settled WR. PAPER ONLY."),
            },
            "markets_feed": self._directional_market_feeds_report(),
            "config_coupling": self._config_coupling_report(),
            "baseline_cohort_gate": self._baseline_cohort_gate_report(),
            "learned_selectivity_gate": self._selectivity_report(),
            "learned_hourly_entry_gate": self._hourly_entry_report(),
            "pre_trade_analysis": self.pre_trade_gate.report(
                evidence=self.pre_trade_evidence),
            "gate_auto_tune": (self.gate_auto_tuner.report()
                              if getattr(self, "gate_auto_tuner", None) is not None
                              else {"enabled": False}),
            "lane_15m_learner": (self.lane_15m_learner.report()
                                if getattr(self, "lane_15m_learner", None) is not None
                                else {"enabled": False}),
            "cross_horizon_learner": (self.cross_horizon_learner.report()
                                     if getattr(self, "cross_horizon_learner", None) is not None
                                     else {"enabled": False}),
            "binary_intel": (self.binary_intel.report()
                             if getattr(self, "binary_intel", None) is not None
                             else {"enabled": False}),
            "sawr": (self.sawr.report()
                     if getattr(self, "sawr", None) is not None
                     else {"enabled": False}),
            "chronos": (self.chronos.report()
                        if getattr(self, "chronos", None) is not None
                        else {"enabled": False}),
            "late_window_entry": self._late_window_report(),
            "tradingview": self._tradingview_report(),
            "tick_reasons": self._reasons,
            "recent_evaluations": self._last_eval,
        }

    def _persist(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            (self._data_dir / "btc_pulse_status.json").write_text(
                json.dumps(self.status(), default=str, indent=1))
            ledger_doc = {**self.ledger.to_dict(),
                          "calibration_state": self.calib.to_state(),
                          "accounting_state": {
                              "directional_learning_version": DIRECTIONAL_LEARNING_VERSION,
                              "lifecycle": self.reconciler.to_state(),
                              "gate_observations": self.gate_obs.to_state(),
                              "ev": {"before_sum": round(self._ev_before_sum, 6),
                                     "after_sum": round(self._ev_after_sum, 6), "n": self._ev_n},
                              "tv_edge": self._tv_edge.to_state(),
                              "rsi_trend": self._rsi_model.to_state(),
                              "tv_learner": self._tv_learner.to_state(),
                              "tv_pending": self._tv_pending[-1000:],
                              "edge_signal": (self.edge_signal.to_state()
                                              if self.edge_signal is not None else {}),
                              "cex_lead": (self.cex_lead.to_state()
                                           if self.cex_lead is not None else {}),
                              "cex_lead_pending": self._cex_lead_pending[-2000:],
                              "mkt_bench_pending": self._mkt_bench_pending[-2000:],
                              "mkt_bench_recent": [list(x) for x in self._mkt_bench_recent],
                              "allowlist_explored": self._allowlist_explored,
                              "allowlist_blocked": self._allowlist_blocked,
                              "research_avoid": sorted(self._research_avoid),
                              "research_exploit": sorted(self._research_exploit),
                              "grok_predictor": (self.grok_predictor.to_state()
                                                 if self.grok_predictor is not None else {}),
                              "grok_analyst": (self.grok_analyst.to_state()
                                               if self.grok_analyst is not None else {}),
                              "grok_decider": (self.grok_decider.to_state()
                                               if self.grok_decider is not None else {}),
                              "llm_council": (self.llm_council.to_state()
                                              if self.llm_council is not None else {}),
                              "council_pending": self._council_pending[-2000:],
                              "grok_news": (self.grok_news.to_state()
                                            if self.grok_news is not None else {}),
                              "grok_pending": self._grok_pending[-2000:],
                              "verifier_pending": self._verifier_pending[-2000:],
                              "recent_windows": self._recent_windows[-40:],
                              "verifier": (self.verifier.to_state() if self.verifier is not None
                                           else {}),
                              "research_loop": (self.research_loop.to_state()
                                                if self.research_loop is not None else {}),
                              "lessons": self.lessons.to_state(),
                              "trade_history": self.trade_history.to_state(),
                              "edge_model": (self.edge_model.to_state()
                                             if self.edge_model is not None else {}),
                              "selectivity_evidence": self.selectivity_evidence.to_state(),
                              "selectivity_gate": self.selectivity_gate.to_state(),
                              "hourly_entry_evidence": self.hourly_entry_evidence.to_state(),
                              "hourly_entry_gate": self.hourly_entry_gate.to_state(),
                              "p_exec_tune": (self.p_exec_tune.to_state()
                                              if getattr(self, "p_exec_tune", None)
                                              is not None else {}),
                              "cell_learning": (self.cell_learning.to_state()
                                                if getattr(self, "cell_learning", None) is not None
                                                else {}),
                              "pre_trade_evidence": self.pre_trade_evidence.to_state(),
                              "pre_trade_gate": self.pre_trade_gate.to_state(),
                              "gate_auto_tuner": (self.gate_auto_tuner.to_state()
                                                  if getattr(self, "gate_auto_tuner", None)
                                                  is not None else {}),
                              "lane_15m_learner": (self.lane_15m_learner.to_state()
                                                   if getattr(self, "lane_15m_learner", None)
                                                   is not None else {}),
                              "cross_horizon_learner": (self.cross_horizon_learner.to_state()
                                                        if getattr(self, "cross_horizon_learner", None)
                                                        is not None else {}),
                              "binary_intel": (self.binary_intel.to_state()
                                               if getattr(self, "binary_intel", None)
                                               is not None else {}),
                              "sawr": (self.sawr.to_state()
                                       if getattr(self, "sawr", None)
                                       is not None else {}),
                              "chronos": (self.chronos.to_state()
                                          if getattr(self, "chronos", None)
                                          is not None else {}),
                              "tv_context_gate": self.tv_context_gate.to_state(),
                              "tv_down_bias_gate": self.tv_down_bias_gate.to_state(),
                              "tv_mtf_gate": self.tv_mtf_gate.to_state(),
                              "down_stack": self.down_stack.to_state(),
                              "late_window_gate": self.late_window_gate.to_state(),
                              "late_window_edge": self.late_window_edge.to_state(),
                              "open_snapshots": self.price.to_open_state(),
                              "eth_open_snapshots": (self._eth_price.to_open_state()
                                                     if self._eth_price is not None else []),
                              "baseline": (self._baseline or empty_baseline()),
                              "report_epoch": dict(self._report_epoch or {})}}
            (self._data_dir / "btc_pulse_ledger.json").write_text(
                json.dumps(ledger_doc, default=str, indent=1))
            from engine.pulse.report_epoch import write_epoch_file
            if self._report_epoch:
                write_epoch_file(self._data_dir, self._report_epoch)
            lr = self.light_report()
            settled_n = int((lr.get("ledger") or {}).get("settled") or 0)
            self._score_history.record(lr.get("scores") or {}, ticks=self.ticks,
                                       settled=settled_n)
            from engine.pulse.report_epoch import filter_score_history
            lr["score_history"] = filter_score_history(
                self._score_history.to_dict(), self._report_epoch)
            (self._data_dir / "btc_pulse_light_report.json").write_text(
                json.dumps(lr, default=str, indent=1))
            self._score_history.save()
            # always write the COMPLETE human-readable performance report (for ChatGPT/Grok review)
            try:
                from engine.pulse.reporting import build_full_report_md
                from engine.pulse.word_report import build_word_report
                st = self.status()
                led = self._ledger_for_report()
                full_md = build_full_report_md(lr, st, led)
                (self._data_dir / "report.md").write_text(full_md, encoding="utf-8")
                (self._data_dir / "FULL_REPORT.md").write_text(full_md, encoding="utf-8")
                build_word_report(lr, status=st, ledger=led,
                                  score_history=lr.get("score_history"),
                                  output_path=self._data_dir / "report.docx")
                (self._data_dir / "LESSONS.md").write_text(self.lessons.to_markdown(),
                                                           encoding="utf-8")
                from engine.pulse.state import build_state_md
                (self._data_dir / "STATE.md").write_text(
                    build_state_md(status=self.status(), ledger=self.ledger.to_dict(),
                                   stop_conditions=self.stop_monitor.report(),
                                   lessons=self.lessons.report()),
                    encoding="utf-8")
                from engine.pulse.provenance import write_provenance_artifacts
                write_provenance_artifacts(
                    self._data_dir, light_report=lr, status=self.status(),
                    ledger=self.ledger.to_dict())
            except Exception:  # noqa: BLE001 — report writing never breaks the loop
                pass
            from engine.pulse.meta_learning import build_bundle
            (self._data_dir / "btc_pulse_meta_bundle.json").write_text(
                json.dumps(build_bundle(lr), default=str, indent=1))
        except Exception as exc:  # noqa: BLE001 — persistence never breaks the loop
            logger.debug("pulse persist failed: %s", exc)

    def run(self, *, max_ticks: Optional[int] = None) -> None:
        logger.info("BTC 5-min pulse engine starting (PAPER ONLY) tick=%.1fs size=$%.2f "
                    "min_edge=%.3f", self.cfg.tick_seconds, self.cfg.size_usd, self.cfg.min_edge)
        n = 0
        if self.osmani_loop is not None:
            self.osmani_loop.start()
        try:
            while True:
                t0 = time.time()
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 — one bad tick never kills the loop
                    logger.exception("pulse tick error")
                n += 1
                if max_ticks is not None and n >= max_ticks:
                    return
                time.sleep(max(0.5, self.cfg.tick_seconds - (time.time() - t0)))
        finally:
            if self.osmani_loop is not None:
                self.osmani_loop.stop()
