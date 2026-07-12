#!/usr/bin/env python3
"""Apply loop-engine architecture env on VPS: quant baseline owns trades; TV observe/context ON."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "hermes-agent-main" / "plugins" / "hermes-trading-engine"
sys.path.insert(0, str(ENGINE_ROOT))

from engine.pulse.config_coupling import (  # noqa: E402
    evaluate_context_cohort_coupling,
    window_seconds_for_slugs,
)


def _resolve_env_path() -> Path:
    profile_path = ROOT / "scripts" / "bot-profile.json"
    if profile_path.exists():
        try:
            prof = json.loads(profile_path.read_text(encoding="utf-8"))
            vps_repo = (prof.get("vps_repo") or "").strip()
            if vps_repo:
                candidate = Path(vps_repo) / "hermes-agent-main/plugins/hermes-trading-engine/.env"
                if candidate.exists() or Path(vps_repo).exists():
                    return candidate
        except (json.JSONDecodeError, OSError):
            pass
    for candidate in (
        Path("/opt/Bot-3/hermes-agent-main/plugins/hermes-trading-engine/.env"),
        Path("/opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine/.env"),
        ENGINE_ROOT / ".env",
    ):
        if candidate.exists():
            return candidate
    return ENGINE_ROOT / ".env"


ENV_PATH = _resolve_env_path()

# FROZEN (operator lock 2026-06-27): TV gate keys in UPDATES below marked [TV-LOCK] must not be
# re-enabled in babysit/autopilot fixes. See .grok/rules/tv-observe-only-lock.md

UPDATES = {
    "PULSE_DASHBOARD_BOT_LABEL": "Bot 3 Directional",
    # VPS public endpoints (TradingView delivers to :80 via hermes-trading-engine proxy).
    "PULSE_DASHBOARD_PUBLISH": "0.0.0.0:80",
    "TRADINGVIEW_WEBHOOK_PUBLISH": "127.0.0.1:18787",
    "TRADINGVIEW_WEBHOOK_MIRROR_URL": "",
    # LLM COUNCIL wiring (operator 2026-07-01 "utilize computing power of Grok and Claude"):
    # Grok is back to SHADOW so it is NOT a solo fail-closed gate (which was blocking trades); instead
    # it feeds the council as a graded MEMBER (its p_up view). The council blends quant + Grok + Claude
    # by live accuracy and drives the trade; the Claude verifier remains the independent checker.
    "PULSE_GROK_DECIDER_MODE": "shadow",
    "PULSE_GROK_DECIDER_EXPLORE_RATE": "0",
    # Both LLMs' compute drives the decision: Grok member + Claude second-opinion member + quant.
    "PULSE_LLM_COUNCIL_ENABLED": "1",
    # CONVICTION BAR (2026-07-02): directional was a coin flip (50.9% WR, edge~0) because the council
    # traded 774/934 windows at min_margin 0.01 -- essentially no conviction required. Raise the bar so
    # it only trades high-conviction consensus (fewer trades, higher WR). Pairs with the cold-member
    # fix (unproven members no longer swing the vote), so a real margin now means the graded members
    # actually agree. 0.62 agreement + 0.05 margin (=P>=0.55 or <=0.45).
    # HIGH-WR ON with loosened parameters (2026-07-11): WR scoreboard + favorites bias,
    # but council/tier/sweet bands below the old 0.70/0.12 + 0.47-0.55 choke.
    "PULSE_HIGH_WR_MODE": "1",
    "PULSE_LLM_COUNCIL_MIN_AGREEMENT": "0.62",
    "PULSE_LLM_COUNCIL_MIN_MARGIN": "0.08",
    "PULSE_LLM_COUNCIL_MIN_MEMBERS": "2",
    # Best-EV side selection (2026-07-01 "do it"): the council picks the side with max (P(side)-ask)
    # instead of the favorite-by-probability. Takes the CHEAP underdog when it's underpriced (high
    # reward/risk, clears the price cap) and refuses to overpay for the favorite -> unchokes fills.
    "PULSE_COUNCIL_MIN_EXECUTABLE_MARGIN": "0.04",
    "PULSE_COUNCIL_BEST_EV": "1",
    # TV per-timeframe council members (2026-07-01): EACH TradingView timeframe (tv_5m, tv_10m,
    # tv_15m, tv_60m, ...) is its own graded council member; the council FOLLOWS/FADES/IGNORES each TF
    # from its OWN live accuracy. Short 2m retired; 3m/4m BTC+ETH wired for observe-only learning
    # (forward-return grading, per-TF ladder, council tv_3m/tv_4m). Tier trade ladder unchanged (5m+).
    "PULSE_COUNCIL_TV_MEMBER": "1",
    # TV freshness cap (2026-07-02): the bot bets at the 15m window OPEN (:00/:15/:30/:45). 5m/15m
    # alerts close on that grid (fresh ~11s); 10m/1h are off-grid and go stale (a 1h read is 15-45 min
    # old at :15/:30/:45). Cap a per-TF read's age to ~1 window so stale/misaligned reads stop voting.
    "PULSE_TV_COUNCIL_MAX_AGE_S": "3600",
    # Operator 2026-07-06 "remove claude": all Anthropic/Claude usage OFF (decider, verifier, dep-arb
    # verifier, MC scenario, research loop). Grok + quant + deterministic MC remain.
    "PULSE_CLAUDE_DECIDER_ENABLED": "0",
    # Monte Carlo: DIRECTIONAL only — Grok parameterizes GBM (vol/drift/jumps); code simulates
    # P(close>=open). Dep-arb / dutch-book MC paths removed. Graded via p_exec self-tune.
    "PULSE_MC_ENABLED": "1",
    "PULSE_MC_PATHS": "8000",
    "PULSE_DIR_MC_ENABLED": "1",
    "PULSE_DIR_MC_PATHS": "8000",
    "PULSE_DIR_MC_CONTROL_ALPHA": "0.5",
    "PULSE_DIR_MC_CRASH_CAP": "0.25",
    "PULSE_P_EXEC_ENABLED": "1",
    "PULSE_P_EXEC_MIN_VWAP": "0.50",
    "PULSE_P_EXEC_EXPLORE_RATE": "0.05",
    "PULSE_P_EXEC_MIN_PROMOTE_N": "40",
    "PULSE_P_EXEC_GATE_COLD": "0",
    "PULSE_MC_SCENARIO_LLM": "1",
    # Dual-LLM MC scenario: Claude OFF (operator 2026-07-06). Grok MC scenario remains.
    "PULSE_MC_SCENARIO_CLAUDE": "0",
    "PULSE_GROK_DECIDER_MIN_CONFIDENCE": "0.62",
    "PULSE_GROK_DECIDER_EXPLORE_MIN_VIEW_MARGIN": "0.08",
    # Trinity profile: fast 15s tick (arb) + tiered Grok (profit/API/soak balance).
    "GROK_BUDGET_DAILY_USD": "35",
    "GROK_EST_USD_PER_CALL": "0.02",
    # Non-learning observe stack OFF (2026-07-05): analyst/predictor/news/overlay/cex-lead never
    # feed loop_synthesis auto-apply, selectivity, council grades, or research avoid/exploit.
    "GROK_SIGNAL_PREDICTOR_ENABLED": "0",
    "GROK_SIGNAL_ANALYST_ENABLED": "0",
    "PULSE_GROK_NEWS_ENABLED": "0",
    "GROK_OVERLAY_ENABLED": "0",
    "PULSE_CEX_LEAD_ENABLED": "0",
    "HERMES_SIGNAL_ENGINE_ENABLED": "0",
    "GROK_PREDICTOR_MAX_CALLS_PER_HOUR": "60",
    "GROK_ANALYST_MAX_CALLS_PER_HOUR": "4",
    "PULSE_GROK_DECIDER_MAX_CALLS_PER_HOUR": "120",
    "PULSE_GROK_DECIDER_TIMEOUT_S": "18",
    # Deep-tier web/X search OFF (operator-authorized 2026-06-30): it drove the decider's timeout
    # errors (~29% of calls, 8.3s avg latency vs 18s cap) and burned API budget for no proven gain
    # (the decider is shadow/observe-only and was anti-predictive). Tiered compute keeps the decider
    # running for grading; it just stops issuing slow live-search calls.
    "PULSE_GROK_DECIDER_USE_SEARCH": "0",
    "PULSE_GROK_NEWS_REFRESH_S": "300",
    "PULSE_GROK_TIERED_COMPUTE": "1",
    "PULSE_GROK_TIER_FULL_DIVERGENCE_MIN": "0.025",
    "PULSE_GROK_TIER_DEEP_DIVERGENCE_MIN": "0.04",
    # Operator 2026-07-06 "remove claude": the Claude maker-checker verifier is OFF (execution gate
    # + quant selectivity remain the trade authority).
    "PULSE_VERIFIER_ENABLED": "0",
    # Council pairs Claude as a voting MEMBER with Claude the verifier (checker). To avoid Claude
    # double-gating (member + fail-closed checker) starving council trades, the verifier now only
    # blocks on an ACTIVE veto, not on a pending/latency verdict (fail-open on pending).
    "PULSE_VERIFIER_FAIL_OPEN": "1",
    "PULSE_VERIFIER_FOLLOW_REQUIRE_VERDICT": "0",
    # [TV-LOCK] observe-only — webhooks feed features/Grok; no MTF or signal trade authority.
    "PULSE_TRADINGVIEW_SIGNAL_GATE": "0",
    "PULSE_TV_EVENT_ID_SUFFIX": "bot3",
    "PULSE_TV_MIN_SIGNAL_STRENGTH": "0",
    "PULSE_TV_MTF_CONFLICT_GATE": "1",
    "PULSE_TV_MTF_REQUIRE_CONFIRM": "0",
    "PULSE_TV_MTF_REQUIRE_ALL_CONFIRM": "0",
    "PULSE_TV_MTF_REQUIRE_SIDE_ALIGN": "0",
    # UP restrictor floors: block proven-losing UP contexts.
    "PULSE_TV_DOWN_BIAS_GATE": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_AGAINST_CONFIRMED_DOWN": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_RANGE_TOP": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_MARKOV_CHOP_NOISE": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_LATE_TTC": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_EARLY_TTC": "1",
    "PULSE_TV_DOWN_BIAS_UP_LATE_TTC_MIN_S": "240",
    "PULSE_TV_DOWN_BIAS_UP_EARLY_TTC_MAX_S": "120",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_CVD_NEUTRAL": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_LOW_CONVICTION": "1",
    "PULSE_TV_DOWN_BIAS_UP_MIN_CONVICTION": "0.40",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_NEUTRAL_ZSCORE": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_MEDIUM_CONFIDENCE": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_UNDERDOG_ENTRY": "1",
    "PULSE_TV_DOWN_BIAS_UP_UNDERDOG_ENTRY_MAX": "0.55",
    # HOURLY THROUGHPUT (2026-07-09): target ~1 fill/hour/symbol (BTC+ETH). Late-window OFF so
    # early/mid hour can trade; GateAutoTuner re-tightens from settled WR if bleed appears.
    "PULSE_LATE_WINDOW_ENTRY": "0",
    "PULSE_LATE_WINDOW_MAX_TTC_S": "3300",
    "PULSE_LATE_WINDOW_MIN_CONVICTION": "0.20",
    # Must exceed scaled cohort max (15m: 220*3+1=661). Coupling auto-clamps if too low.
    "PULSE_TV_CONTEXT_MAX_TTC_S": "900",
    "PULSE_TV_CONTEXT_EXPLORATION_RATE": "0",
    "PULSE_TV_DOWN_BIAS_EXPLORE_RATE": "0",
    # Strict abstention: only proven buckets trade; 5% exploration builds clean forward evidence.
    "PULSE_DIRECTIONAL_REQUIRE_WINNING": "0",
    "PULSE_DIRECTIONAL_EXPLORE_RATE": "0.12",
    "PULSE_DIRECTIONAL_WINNING_MIN_SAMPLES": "30",
    # Conservative base edge; fee-aware execution adds the separate net-EV floor below.
    "PULSE_MIN_EDGE": "0.025",
    "PULSE_BASIS_BUFFER": "0.005",
    "PULSE_EDGE_BUFFER": "0.005",
    # Reject wide books; paying 8 cents of spread cannot support a selective directional edge.
    "PULSE_EXEC_MAX_SPREAD": "0.04",
    # Require 1.5 points after VWAP and taker fees, before any paper fill.
    "PULSE_EXEC_MIN_EV": "0.015",
    # Align with triage/tier sweet band (0.48–0.72); was 0.52 blocking 47–51¢ tickets.
    "PULSE_MIN_ENTRY_PRICE": "0.45",
    # Favorites have lower R:R; 0.25 allows entries up to ~0.80 while still skipping dust.
    "PULSE_MIN_REWARD_RISK": "0.25",
    "PULSE_MIN_REWARD_RISK_UP_PREMIUM": "0.10",
    "PULSE_GROK_UP_MIN_P_WIN": "0.55",
    # The source observation must be within 3s of the true boundary; otherwise abstain.
    "PULSE_MAX_OPEN_LAG_S": "3",
    "PULSE_MAX_OPEN_LAG_15M_S": "3",
    "HERMES_SETTLEMENT_SOURCE_PRIORITY": "polymarket_resolution",
    # Stop halt: keep above rolling_n until post-relaxation cohort rebuilds (n=50 was frozen).
    "PULSE_STOP_MIN_SAMPLES": "60",
    # Sweet-spot entry (1M MC sim): base 160-220s → 15m TTC 480-660s (minutes 8-11).
    "PULSE_TICK_SECONDS": "15",
    # Widened 0.65->0.72 (2026-07-02) as a CAPPED favorite-band experiment: the favorite-longshot-bias
    # literature says favorites (0.60-0.85) resolve MORE often than their price implies (Leo Labs: 0.60-
    # 0.70 -> ~80% realized). We have only ~4 trades there, so this is UNVERIFIED for our BTC markets ->
    # it stays an experiment, not a strategy flip. Raising the cap only ENABLES best-EV to take a
    # favorite when it judges it underpriced (needs consensus p_up > price + reward/risk), which is rare
    # since quant is ~calibrated -> naturally self-capping. Per-price-bucket realized-vs-implied WR is
    # tracked on the dashboard so we measure whether the favorite band actually pays here.
    "PULSE_MAX_PRICE": "0.85",
    # TV context gate ON (restrict-only): blocks proven-losing contexts; not trade authority.
    "PULSE_TV_CONTEXT_GATE": "1",
    # TV confidence tier: modulate min_edge/max_price at 15m sweet spot (not a trade gate).
    "PULSE_TV_CONFIDENCE_TIER_ENABLED": "1",
    "PULSE_TV_TIER_REQUIRE_SWEET_SPOT": "1",
    "PULSE_TV_TIER_15M_ONLY": "0",
    "PULSE_TV_TIER_ALIGNED_STRENGTH_MIN": "0.72",
    "PULSE_TV_TIER_A_MIN_EDGE_DELTA": "-0.005",
    "PULSE_TV_TIER_A_MAX_PRICE_DELTA": "0.02",
    "PULSE_TV_TIER_C_MIN_EDGE_DELTA": "0.005",
    "PULSE_TV_TIER_C_MAX_PRICE_DELTA": "-0.03",
    # Mispricing/edge-TTC off on quant baseline (Grok shadow; redundant with cohort).
    "PULSE_MISPRICING_GATE_ENABLED": "0",
    "PULSE_MISPRICING_TTC_MIN_S": "160",
    "PULSE_MISPRICING_TTC_MAX_S": "220",
    "PULSE_MISPRICING_REQUIRE_CONFIRMED": "0",
    "PULSE_MISPRICING_REQUIRE_STALE_DOWN": "1",
    "PULSE_MISPRICING_MIN_EXECUTABLE_MARGIN": "0.02",
    "PULSE_MISPRICING_FOLLOW_ON_ABSTAIN": "0",
    "PULSE_MISPRICING_FOLLOW_SIZE_FRACTION": "0.5",
    "PULSE_EDGE_TTC_GATE_ENABLED": "0",
    "PULSE_CEX_LEAD_MIN_EDGE_VS_MARKET": "0.02",
    "PULSE_CEX_LEAD_TV_STRENGTH_THR": "0.72",
    # Tier 1: sweet-spot cohort 160-220s base (15m fast-lane → 480-660s TTC).
    # Baseline cohort gate OFF for 1h directional: the 15m-scaled TTC band (150-240s * 12) blocks
    # early-window entries (~3540s TTC at 60s after open). Council + execution gate still bound quality.
    "PULSE_BASELINE_COHORT_GATE_ENABLED": "0",
    "PULSE_BASELINE_COHORT_TTC_MIN_S": "160",
    "PULSE_BASELINE_COHORT_TTC_MAX_S": "230",
    "PULSE_BASELINE_COHORT_REQUIRE_HIGH_EDGE": "0",
    "PULSE_BASELINE_COHORT_REQUIRE_STRONG_CEX": "0",
    "PULSE_BASELINE_COHORT_15M_FAST_LANE": "1",
    "PULSE_BASELINE_COHORT_15M_TTC_MIN_S": "150",
    "PULSE_BASELINE_COHORT_15M_TTC_MAX_S": "240",
    # [TV-LOCK] baseline path does not use TV stack to block entries.
    "PULSE_BASELINE_UP_TV_GATE_ENABLED": "0",
    "PULSE_BASELINE_DOWN_TV_GATE_ENABLED": "0",
    "PULSE_BASELINE_DOWN_BLOCK_BULLISH_RANGE": "1",
    "PULSE_BASELINE_DOWN_BLOCK_UP_STRONG_BULLISH": "1",
    "PULSE_BASELINE_DOWN_BLOCK_NOT_STALE": "0",
    "PULSE_BASELINE_DOWN_BLOCK_MEDIUM_EDGE": "0",
    "PULSE_BASELINE_DOWN_BLOCK_SINGLE_TF": "0",
    "PULSE_BASELINE_DOWN_BLOCK_VOLUME_ACTIVE": "0",
    "PULSE_BASELINE_DOWN_BLOCK_BULLISH_MTF": "0",
    "PULSE_BASELINE_DOWN_BLOCK_MID_ENTRY": "0",
    "PULSE_BASELINE_DOWN_BLOCK_BB_EXPANSION_DOWN": "0",
    "PULSE_BASELINE_DOWN_MID_ENTRY_MIN": "0.55",
    "PULSE_BASELINE_DOWN_MID_ENTRY_MAX": "0.60",
    # Directional-only scan surface — BTC/ETH 1h + 15m (no 5m brain / arb series).
    "PULSE_SERIES_SLUGS": (
        "btc-up-or-down-hourly,eth-up-or-down-hourly,"
        "btc-up-or-down-15m,eth-up-or-down-15m"),
    # Directional lanes: hourly BTC/ETH + separate 15m BTC/ETH (light gates; lane learner raises WR).
    "PULSE_DIRECTIONAL_SERIES_SLUGS": (
        "btc-up-or-down-hourly,eth-up-or-down-hourly,"
        "btc-up-or-down-15m,eth-up-or-down-15m"),
    "PULSE_DIRECTIONAL_HOURLY_DISCOVER": "1",
    "PULSE_DIRECTIONAL_15M_DISCOVER": "1",
    "PULSE_DIRECTIONAL_EVENT_SLUGS": "",
    # Disabled: this legacy learner pools BTC/ETH and UP/DOWN into one mutable policy.
    "PULSE_LANE_15M_LEARN_ENABLED": "1",
    "PULSE_LANE_15M_TARGET_WR": "0.60",
    "PULSE_LANE_15M_KILL_WR": "0.45",
    "PULSE_LANE_15M_MIN_SAMPLES": "10",
    # Shared 15m↔1h cross-horizon learner (restrict/size only; execution gate authoritative).
    # Locked — redesign only with operator approval (.grok/rules/cross-horizon-learn-lock.md).
    "PULSE_CROSS_HORIZON_LEARN_ENABLED": "1",
    "PULSE_CROSS_HORIZON_MIN_SAMPLES": "20",
    "PULSE_CROSS_HORIZON_TARGET_WR": "0.60",
    "PULSE_CROSS_HORIZON_KILL_WR": "0.45",
    "PULSE_CROSS_HORIZON_EXPLORATION_RATE": "0.08",
    # Phase 1 directional cell learning table (observe-only grading).
    "PULSE_CELL_LEARNING_ENABLED": "1",
    "PULSE_CELL_LEARNING_MIN_SAMPLES": "30",
    # Phase 2 cell nudge OFF (2026-07-11): was downgrading tier snipe/harvest to wait.
    "PULSE_CELL_LEARNING_PHASE2_ENABLED": "0",
    # Hourly band: enter from ~15m into the hour (last 6×15m bar-close lean) through ~55m.
    # Chart-lean hard gate blocks opposed short-term path; GateAutoTuner may raise SSO later.
    # Hourly entry gate OFF (2026-07-11 loosen): was blocking 25+ discovery paths; tier still gates.
    "PULSE_HOURLY_ENTRY_GATE_ENABLED": "0",
    "PULSE_HOURLY_MIN_SECONDS_SINCE_OPEN": "300",
    "PULSE_HOURLY_MAX_SECONDS_SINCE_OPEN": "3300",
    "PULSE_HOURLY_ENTRY_MIN_SAMPLES": "20",
    "PULSE_HOURLY_ENTRY_EXPLORATION_RATE": "0.20",
    "PULSE_HOURLY_ENTRY_MIN_PROFIT_FACTOR": "0.85",
    "PULSE_HOURLY_ENTRY_FDR_Q": "0.10",
    "PULSE_HOURLY_ENTRY_CONFIDENCE_Z": "1.64",
    # Pre-trade: lower readiness bar + explore so hourly BTC/ETH can fill.
    "PULSE_PRE_TRADE_ANALYSIS_ENABLED": "1",
    "PULSE_PRE_TRADE_MIN_SCORE": "0.25",
    "PULSE_PRE_TRADE_MARGIN_BOOST_MAX": "0.04",
    "PULSE_PRE_TRADE_AGREEMENT_BOOST_MAX": "0.05",
    "PULSE_PRE_TRADE_EXPLORATION_RATE": "0.08",
    "PULSE_PRE_TRADE_MIN_SIZE_SCALE": "0.35",
    "PULSE_PRE_TRADE_HOURLY_MIN_MINUTES": "15",
    "PULSE_PRE_TRADE_EVIDENCE_MIN_SAMPLES": "20",
    # TV strong-fade observe-only for throughput; auto-tuner + selectivity still protect WR.
    "PULSE_TV_STRONG_FADE_ENABLED": "0",
    "PULSE_TV_STRONG_FADE_EXEMPT_TIER_SNIPE": "1",
    # Re-enabled 2026-07-12 for Bot-3 sample starvation recovery (FULL_REPORT n=9, tier choke).
    # Mutations stay in-process; recreate still resets — acceptable vs permanent WAIT starvation.
    "PULSE_GATE_AUTO_TUNE_ENABLED": "1",
    "PULSE_GATE_AUTO_TUNE_LOOKBACK_N": "24",
    "PULSE_GATE_AUTO_TUNE_MIN_SAMPLES": "12",
    "PULSE_GATE_AUTO_TUNE_TARGET_WR": "0.65",
    "PULSE_GATE_AUTO_TUNE_KILL_WR": "0.50",
    "PULSE_GATE_AUTO_TUNE_STARVE_FPH": "0.8",
    "PULSE_GATE_AUTO_TUNE_RICH_FPH": "3.0",
    "PULSE_GATE_AUTO_TUNE_COOLDOWN": "6",
    # DOWN overconfidence filter (ask_down - fair_p_up); blocks FULL_REPORT loser cluster.
    "PULSE_DOWN_MAX_ASK_FAIR_GAP": "0.12",
    # Quant-only tier fallback when TradingView MTF is absent (sparse alerts / cold start).
    "PULSE_TIER_QUANT_ONLY_WHEN_NO_TV": "1",
    "PULSE_TIER_QUANT_ONLY_MIN_EDGE": "0.02",
    "PULSE_TIER_QUANT_ONLY_MIN_CONVICTION": "0.08",
    "PULSE_TIER_HARVEST_EDGE_MIN": "0.02",
    # Loss-streak size cut (Osmani maker).
    "PULSE_LOSS_STREAK_CUT_AFTER": "2",
    "PULSE_LOSS_STREAK_CUT_TRADES": "5",
    "PULSE_LOSS_STREAK_SIZE_MULT": "0.5",
    # Cost-aware capture (deep-scan 2026-06-29, operator-authorized): the flat 0.015 epsilon
    # double-counted execution risk and never fired on tight BTC books. We now make the
    # PER-OPPORTUNITY non-atomic sim the real cost filter (market impact + 50bps leg-2 slippage +
    # pre-commit-breach check) and drop epsilon to a small fees-only floor. Net effect: capture the
    # near-miss band ONLY when the trade still books guaranteed >0 after realistic sequential fills;
    # reject sub-cost ones. Every booked arb stays guaranteed >= $0 by construction.
    # Atomic within-window arb RE-ENABLED (operator-authorized 2026-07-01): it is the only
    # GUARANTEED positive-EV lane (buy up+down for <$1, collect exactly $1 — risk-free by
    # construction). Runs alongside the Claude-gated dep-arb so there is a real profitable lane while
    # dep-arb's verifier matures. Directional stays OFF (PULSE_DIRECTIONAL_ENABLED=0). Net: arb-only
    # (atomic risk-free arb + dep-arb conjunction/Claude-gated nested), no directional noise.
    # Loosened 2026-07-02 ("loosen Arb land a bit"): all rejects were below_epsilon (ask-sum ~$1.00),
    # so halve the arbitrary buffer above fees -> capture thinner but still genuinely risk-free edges.
    # STILL requires ask_sum < 1 - fees - epsilon (positive margin) + survives the non-atomic slippage
    # sim, so we never book a guaranteed loss (ask_sum >= $1). Not lowered to 0 to avoid dust trades.
    # Tightened 0.0005 -> 0.0002 (2026-07-05, live scan): 1,395 near-misses clustered at 0.00-0.02
    # residual (ask/bid VWAP within 2c of $1) but still below_epsilon; halving the buffer admits
    # crossings at ask_sum < 0.9998 / bid_sum > 1.0002. Non-atomic sim + positive post-fee margin
    # remain the hard guards — never book ask_sum >= $1.
    # Lowered 0.03 -> 0.025 (2026-07-02): admit slightly thinner LCMM violations for more volume; the
    # MC +EV gate + entry floor still filter adverse selection.
    # WS3-B: Fréchet conjunction floor — the only dep-arb path that may EXECUTE. It is true
    # risk-free arb (all nested children UP => parent UP), so it stays ON.
    # Nested-implication execution is ON but now GATED by an AUTHORITATIVE Claude verifier
    # (operator-authorized 2026-06-30 "strengthen dep-arb"). The raw nested heuristic is negative-EV
    # (capture -0.18, holdout PF 0.78) and previously ran fail-OPEN — Claude only graded after the
    # fact, so it bled -$406. With FAIL_OPEN=0 + REQUIRE_VERDICT=1 below, a nested fill now requires
    # an EXPLICIT Claude approve; pending/veto/error => no trade. Claude's counterfactual vetoes have
    # been correct (avoided -$100), so making it the gatekeeper stops the bleed while keeping the
    # LLM-leveraged path open to scale as the verifier's veto_quality proves out.
    # Dep-arb scalp learning: conjunction-only + sweet entry + mid-exit (train on settled evidence).
    # Clock-skew off: parent books refresh every tick (~15s) so min_parent_book_age_s=120 starved fills.
    # Bucket-bleeding halt threshold: relax to 0.90 so break-even bands can still trade while MC selects.
    # Raised 0.70 -> 0.85 (2026-07-02): the fixed cap was blocking MC-APPROVED +EV opportunities above
    # 0.70 (e.g. entry 0.80 with MC p_parent_up=0.94 -> +0.14 EV/$). The MC +EV gate (graded) is the
    # intelligent arbiter now, so let it decide up to 0.85; keep a ceiling to avoid the extreme-price
    # fragility (breakeven WR ~0.85, one loss wipes many wins). Floor 0.50 + MC gate remain the guards.
    # Sweet scalp band: cheap parent-UP (<0.52) and tail (>0.72) bleed on hold-to-resolution.
    # Grok 60s convergence predictor DISABLED (operator-authorized 2026-06-30): live accuracy was 4%
    # (worse than random — an anti-signal), yet it was fed into the Claude dep-arb verifier as a prior
    # (engine build_dep_arb_verify_payload grok_convergence=), degrading veto quality. Turning it off
    # removes the misleading prior AND frees Grok daily budget for the dependency proposer (which was
    # skipped_budget-starved at 0 validated proposals).
    # Operator 2026-07-06 "remove claude": Claude dep-arb verifier OFF (MC +EV gate remains the arbiter).
    # FAIL-OPEN now (2026-07-02 "make dep-arb trade"): the verifier was fail-CLOSED + require-verdict,
    # so Claude credit errors / timeouts hard-VETOED every fill (dep_arb_verifier_veto=212). Worse, its
    # veto_quality graded "vetoes_costing_edge" (vetoed trades would've won 55.6% / +$2.67). Since the
    # MC +EV gate (deterministic, graded) is now the real profitability arbiter, make Claude ADVISORY:
    # no verdict / error / timeout => APPROVE (don't block). MC + entry floor stay the hard guards.
    # ON (operator 2026-07-04 "turn all trading lanes on again"). NOTE: directional graded ~coin-flip
    # (50% WR, edge~0) previously; the learned selectivity/edge gates still bound quality and grade it.
    "PULSE_DIRECTIONAL_ENABLED": "1",
    "PULSE_DIRECTIONAL_MAX_BANKROLL_FRAC": "0.35",
    # Unchoke UP (operator 2026-07-01 "let bot trade"): the market currently leans UP, and every UP
    # entry was blocked by BLOCK_UP_UNTIL_PROMOTED (rejected:up_blocked_until_promoted was the binding
    # choke after the edge relax). Open the UP side so the council/quant can actually trade its
    # direction; the execution-quality EV gate + calibration + selectivity learners still bound quality
    # and GRADE UP on real outcomes (re-block via the learner if UP proves losing). Was proven-marginal.
    "PULSE_DIRECTIONAL_DOWN_ONLY": "0",
    "PULSE_DIRECTIONAL_BLOCK_UP_UNTIL_PROMOTED": "0",
    "PULSE_DIRECTIONAL_UP_RESTRICTIONS_ENABLED": "0",
    # Operator 2026-07-11: directional-only — plain arb + dep-arb execution fully OFF.
    "PULSE_ARB_ENABLED": "0",
    "PULSE_ARB_SCAN_SECONDS": "0",
    "PULSE_ARB_EXTRA_SERIES_SLUGS": "",
    "PULSE_ARB_GLOBAL_MAX_OPEN_USD": "0",
    "PULSE_DEPENDENCY_ARB_ENABLED": "0",
    "PULSE_DEPENDENCY_ARB_EXECUTE": "0",
    "PULSE_DEPENDENCY_ARB_CONJUNCTION": "0",
    "PULSE_DEPENDENCY_ARB_NESTED_EXECUTE": "0",
    "PULSE_DEPENDENCY_ARB_MID_EXIT_ENABLED": "0",
    "PULSE_DEPENDENCY_ARB_EXPERIMENT_AUTO_APPLY": "0",
    "PULSE_GAMMA_CRYPTO_ARB_ENABLED": "0",
    "PULSE_MC_DEP_ARB_GATE": "0",
    "PULSE_GROK_DEPENDENCY_ENABLED": "0",
    "PULSE_STOP_DEP_ARB_GUARD_ENABLED": "0",
    "PULSE_DEP_ARB_VERIFIER_ENABLED": "0",
    "DISABLE_ARBITRAGE_TRADING": "1",
    "PULSE_GREEN_PATH_ENABLED": "0",
    # Bregman / IP-oracle modules deleted — force OFF so stale VPS env cannot revive them.
    "PULSE_BREGMAN_PROJECTION_ENABLED": "0",
    "PULSE_BREGMAN_TRADE_AUTHORITY": "0",
    "PULSE_CLOB_WEBSOCKET_ENABLED": "1",
    "PULSE_STOP_MIN_SHARPE": "0",
    "PULSE_STOP_SHARPE_MIN_SAMPLES": "20",
    # Dep-arb stop guard ON (2026-07-07): re-enabled after disabling asymmetric bleed path and
    # hard scalp gates. Halts new LCMM entries when realized P&L < 0 (blocks further bleed).
    # ETH 5m/15m on scan surface (15m directional also via DIRECTIONAL_SERIES_SLUGS).
    "PULSE_ETH_SERIES_ENABLED": "1",
    # Operator 2026-07-06 "remove claude": the Claude research meta-loop (lesson generation) is OFF.
    "PULSE_RESEARCH_LOOP_ENABLED": "0",
    "PULSE_RESEARCH_AUTO_APPLY": "1",
    "PULSE_RESEARCH_INTERVAL_S": "1200",
    "PULSE_RESEARCH_AVOID_MAX": "20",
    "PULSE_RESEARCH_FORBID_SIZE_INCREASE": "1",
    "PULSE_LEARNING_ENABLED": "1",
    # LOCKS LIFTED (operator 2026-07-01): activate the learning blend sooner (was 40; n=24 collected)
    # so the learned edge model starts steering fair value now instead of staying observe-only.
    "PULSE_LEARNING_MIN_SAMPLES": "20",
    "PULSE_LEARNING_RAMP_SAMPLES": "120",
    "PULSE_LEARNING_BENCH_MARGIN": "0.0",
    # Fast inter-tick arb scan (2026-07-04, MC Config D): real risk-free crossings are brief and the 15s
    # tick misses them. Re-scan (taker only) every 5s between ticks on the same windows (books refreshed
    # via REST; stale-book guard still gates). ~3x the crossing-catch rate; MC: ~26d -> ~18d to 100x.
    # Safe: never books stale data, one-arb-per-window unchanged. 5 -> 2 (2026-07-04): the WS live-book
    # TRIGGER now makes each rescan nearly free (reads the real-time WS book; only REST-confirms a
    # near-crossing), so a tight 2s cadence catches more transient crossings without REST load.
    # Gamma API surface scanners (crypto dutch/simplex + title-linked dep-arb; observe-only).
    # Osmani 2026 loop engineering — 3 decoupled lanes (discovery / execution / ledger).
    "PULSE_OSMANI_LOOP_ENABLED": "1",
    "PULSE_OSMANI_DISCOVERY_INTERVAL_S": "60",
    "PULSE_OSMANI_TRIAGE_SKILL_ENABLED": "1",
    # PRISM (Posterior-Ranked Information State Machine) — observe-only rank R = I*max(0,E)*C.
    # Ensemble edge is computed for the status API; the stopping gate stays OFF (never tightens the
    # live Osmani directional path) until explicitly promoted after evidence review.
    "PULSE_PRISM_ENABLED": "1",
    "PULSE_PRISM_MC_PATHS": "20000",
    "PULSE_PRISM_TV_DRIFT_SCALE": "0.30",
    "PULSE_PRISM_SNIPER_R_MIN": "0.12",
    "PULSE_PRISM_HARVESTER_R_MIN": "0.03",
    "PULSE_PRISM_I_FLOOR_SNIPER": "0.70",
    "PULSE_PRISM_DAILY_LOSS_HALT_PCT": "0.12",
    "PULSE_PRISM_CROSS_ASSET": "1",
    # PRISM stays OBSERVE-ONLY: the ensemble/agents/cross-asset all publish to the status API, but
    # the stopping/Thompson/agent gates + BNB block stay OFF so the live Osmani directional path is
    # not tightened. Promote these to 1 only after evidence shows PRISM beats baseline.
    "PULSE_PRISM_STOPPING_ENABLED": "0",
    "PULSE_PRISM_THOMPSON_GATE_ENABLED": "0",
    "PULSE_PRISM_AGENT_GATE_ENABLED": "0",
    "PULSE_PRISM_BNB_BLOCK": "0",
    "PULSE_TRIAGE_MIN_DEPTH_USD": "50",
    "PULSE_TRIAGE_MAX_SLIPPAGE_PCT": "2",
    "PULSE_TRIAGE_MIN_SHARES": "5",
    # Per-asset triage parity: ETH hourly uses the same thresholds as BTC (override per key if needed).
    "PULSE_TRIAGE_BTC_MIN_DEPTH_USD": "50",
    "PULSE_TRIAGE_BTC_MAX_SLIPPAGE_PCT": "2",
    "PULSE_TRIAGE_BTC_MIN_SHARES": "5",
    # Throughput sweet band 0.45-0.85 (BTC+ETH). GateAutoTuner raises floor if WR bleeds.
    "PULSE_TRIAGE_BTC_SWEET_MIN": "0.48",
    "PULSE_TRIAGE_BTC_SWEET_MAX": "0.72",
    # Allow mild tail path for learning (was killed at 0.01).
    "PULSE_TRIAGE_BTC_TAIL_MAX": "0.08",
    "PULSE_TRIAGE_BTC_TAIL_MIN_STRENGTH": "0.70",
    "PULSE_TRIAGE_BTC_TV_MAX_AGE_S": "3600",
    "PULSE_TRIAGE_ETH_MIN_DEPTH_USD": "50",
    "PULSE_TRIAGE_ETH_MAX_SLIPPAGE_PCT": "2",
    "PULSE_TRIAGE_ETH_MIN_SHARES": "5",
    "PULSE_TRIAGE_ETH_SWEET_MIN": "0.48",
    "PULSE_TRIAGE_ETH_SWEET_MAX": "0.72",
    "PULSE_TRIAGE_ETH_TAIL_MAX": "0.08",
    "PULSE_TRIAGE_ETH_TAIL_MIN_STRENGTH": "0.70",
    "PULSE_TRIAGE_ETH_TV_MAX_AGE_S": "3600",
    # Operator 2026-07-06: loosen the Osmani triage TV freshness window 30m -> 60m so a signal stays
    # valid for the full hourly window (overnight TV-signal sparsity was starving directional entries).
    "PULSE_TRIAGE_TV_MAX_AGE_S": "3600",
    # Osmani TV feature lookup must match triage freshness (was 300s → REJECT_NO_TV_SIGNAL
    # starved ETH/BTC when alerts were 5–60m old but still valid for hourly triage).
    "PULSE_TV_SIGNAL_MAX_FEATURE_AGE_S": "3600",
    # Spot price-action trend (rising/falling/flat) for Osmani triage + Grok — not TV UP/DOWN labels.
    "PULSE_TRIAGE_TREND_SOURCE": "price",
    "PULSE_GROK_TREND_SOURCE": "price",
    # Detect trend sooner — flat windows were starving Discovery (REJECT_TREND_MISALIGNED).
    "PULSE_PRICE_TREND_MIN_MOVE_BPS": "0.2",
    # Allow flat Chainlink trend probes for learning (still restrict-only; not forced fills).
    "PULSE_TRIAGE_FLAT_EXPLORATION_RATE": "0.10",
    # ---- DIRECTIONAL TIER ENGINE (operator 2026-07-06; $2000 bankroll, trade-like-live) ----
    # Regime-conditioned Bayesian tier system is the directional brain. Explicit 1h + 15m feeds
    # use tier engine on tick path (legacy_tick=0 -> unstructured pulse directional OFF).
    # execution_gate remains the sole fill authority. PAPER ONLY.
    "PULSE_TIER_ENGINE_ENABLED": "1",
    "PULSE_DIRECTIONAL_LEGACY_TICK": "0",
    "PULSE_TIER_BANKROLL_USD": "2000",
    # Throughput tier caps (still Kelly-bounded). Auto-tuner does not change size caps.
    "PULSE_TIER_SNIPE_MAX_USD": "25",
    "PULSE_TIER_STRIKE_MAX_USD": "15",
    "PULSE_TIER_HARVEST_MAX_USD": "10",
    "PULSE_TIER_PROBE_USD": "5",
    "PULSE_TIER_SNIPE_Z_MIN": "1.4",
    "PULSE_TIER_STRIKE_EDGE_MIN": "0.02",
    "PULSE_TIER_HARVEST_EDGE_MIN": "0.015",
    "PULSE_TIER_KELLY_FRACTION": "0.25",
    "PULSE_TIER_DAILY_LOSS_HALT_PCT": "0.10",
    "PULSE_TIER_MAX_CONCURRENT": "6",
    # Wide sweet band for hourly BTC+ETH fills; auto-tuner may tighten.
    # HIGH-WR favorites band (loosened): 0.48-0.72 vs old 0.47-0.55; harvest/snipe still active.
    "PULSE_TIER_SWEET_MIN": "0.48",
    "PULSE_TIER_SWEET_MAX": "0.72",
    # 15m: enter after ~2m (scaled SSO); was 300s watching_floor starvation.
    "PULSE_TIER_MIN_SECONDS_SINCE_OPEN": "90",
    "PULSE_TIER_SLIPPAGE_BUFFER": "0.005",
    # $2000 real-money bankroll (paper). Directional slice + capital token set at canonical keys below.
    "PULSE_STARTING_CAPITAL_USD": "2000",
    "PULSE_LOOP_CIRCUIT_BREAKER_ENABLED": "1",
    "PULSE_LOOP_MAX_DAILY_TOKEN_USD": "50",
    "PULSE_LOOP_EST_USD_PER_CALL": "0.02",
    "PULSE_LOOP_MAX_API_CALLS_PER_HOUR": "500",
    "PULSE_LOOP_MIN_ON_HAND_USD": "50",
    "PULSE_LOOP_MAX_DRAWDOWN_PCT": "40",
    "PULSE_LOOP_MAX_LANE_RETRIES": "5",
    "PULSE_LOOP_MAX_CONSECUTIVE_ERRORS": "20",
    # Asymmetric gamma dep-arb paper execution DISABLED (2026-07-07): bypassed LCMM sweet-band,
    # min-entry-vwap, MC, and learning gates — bought parent-UP at 3–19¢ and bled -$80+ in cheap
    # buckets. Gamma dep-arb scanner stays ON (observe-only); LCMM conjunction path is the only executor.
    # TV alerts → immediate arb/dep-arb lane triggers (operator-authorized; not directional gates).
    # Step 2 guard: leg risk is the only way arb can lose — atomic complete-set only.
    # Operator-authorized 2026-06-29: re-enabled so the sim is the PER-OPPORTUNITY cost filter that
    # makes the low epsilon above safe (rejects any near-miss that would lose after leg-2 slippage).
    # Loosened 50 -> 35 -> 28 bps (2026-07-05): with epsilon at 0.0002 the leg-2 slippage buffer is
    # now the binding gate on near-crossings. 28 bps is still conservative on liquid crypto top-of-book
    # but lets sub-0.2% edges survive the non-atomic sim. Revert if any booked arb settles at a loss.
    "PULSE_SIZING_PROMOTION_GATED": "1",
    # LOCKS LIFTED (operator 2026-07-01): dynamic edge-proportional sizing ON (still bounded by the
    # $5/bet dep-arb cap + bankroll caps + FORBID_SIZE_INCREASE, and promotion-gated so it scales
    # only as learned edge proves out).
    "HERMES_SIZING_ENABLED": "1",
    # Osmani directional: bot decides bet size (half-Kelly × pre-trade readiness). PAPER ONLY.
    # Clamped to [PULSE_OSMANI_SIZING_MIN_USD, HERMES_SIZING_HARD_CAP_USD]. No martingale.
    "PULSE_OSMANI_AUTONOMOUS_SIZING": "1",
    "PULSE_OSMANI_SIZING_MIN_USD": "1.0",
    "HERMES_SIZING_HARD_CAP_USD": "10.0",
    "HERMES_SIZING_BANKROLL_USD": "1000.0",
    "HERMES_SIZING_DAILY_LOSS_CAP_USD": "50.0",
    # Bot 3: INDEX:BTCUSD + INDEX:ETHUSD · 5m RSI Divergence only (15m directional lane).
    "PULSE_TV_FEATURE_SYMBOL": "BTCUSD",
    "TRADINGVIEW_ALLOWED_SYMBOLS": "BTCUSD,INDEX:BTCUSD,ETHUSD,INDEX:ETHUSD",
    "TRADINGVIEW_MAX_AGE_S": "3600",
    "PULSE_TV_DROP_TIMEFRAMES": "2,3,4,10,15,20,25,30,35,40,45,50,55,60",
    "PULSE_TV_MTF_TIMEFRAMES": "5",
    "TRADINGVIEW_WEBHOOK_HOST": "0.0.0.0",
    "PULSE_TV_ALERT_HISTORY_PER_SYMBOL": "20",
    "PULSE_TV_RSI_DIV_HISTORY_PER_SYMBOL": "20",
    "PULSE_TV_15M_SHORT_PATH_N": "8",
    "PULSE_TV_15M_CHART_LEAN_ENABLED": "0",
    "PULSE_TV_15M_CHART_LEAN_SIZE": "0",
    "PULSE_TV_1H_SHORT_PATH_N": "12",
    "PULSE_TV_1H_CHART_LEAN_ENABLED": "0",
    "PULSE_TV_1H_CHART_LEAN_GATE": "0",
    "PULSE_TV_1H_CHART_LEAN_SIZE": "0",
    "PULSE_TV_RSI_OVERLAY_ENABLED": "1",
    "PULSE_TV_RSI_OVERLAY_SIZE": "1",
    "PULSE_TV_RSI_OVERLAY_MAX_AGE_S": "2700",
    "PULSE_TV_RSI_OVERLAY_ALIGNED_MULT": "1.15",
    "PULSE_TV_RSI_OVERLAY_OPPOSED_MULT": "0.45",
    "PULSE_TV_RSI_BAND_ENABLED": "0",
    "PULSE_TV_RSI_DIVERGENCE_ANALYSIS_ENABLED": "1",
    # Binary Intel — invented quant math + universal 5m TV + Grok pre/post-trade scripts.
    "PULSE_BINARY_INTEL_ENABLED": "1",
    "PULSE_BINARY_INTEL_GROK_COMPUTE": "1",
    "PULSE_BINARY_INTEL_MIN_SCORE": "0.28",
    "PULSE_BINARY_INTEL_EXPLORATION_RATE": "0.05",
    "PULSE_BINARY_INTEL_MIN_SIZE_SCALE": "0.40",
    "PULSE_BINARY_INTEL_KELLY_FRACTION": "0.25",
    "PULSE_TV_2H_REVIEW_ENABLED": "0",
    "PULSE_TV_2H_LOOKBACK_S": "7200",
    "PULSE_TV_2H_ALERT_HISTORY_CAP": "50",
    "PULSE_TV_2H_REVIEW_PRETRADE": "0",
    "PULSE_TV_2H_COUNCIL_GRADE": "0",
    # Reset new + retired members so the tv_mtf blend recomputes cleanly over the tier ladder.
    "PULSE_TV_RESET_TOKEN": "2026-07-07-tv-3m-4m",
    "PULSE_TV_RESET_MEMBERS": "tv_3m,tv_4m,tv_5m,tv_60m,tv_240m,tv_1440m,tv_mtf",
    # Capital reset token cleared (2026-07-11): one-time cleanup applied; empty = no reset on restart.
    "PULSE_RESET_CAPITAL_TOKEN": "",
    # Cross-lane correlated-exposure cap (2026-07-02): directional UP and dep-arb parent-UP are both
    # long BTC-up; cap the combined same-direction exposure open at once so the 3 lanes don't stack the
    # same bet. Read-only gate (only blocks, never forces). ~$20 allows a normal mix, blocks piling on.
    "PULSE_CORRELATED_EXPOSURE_CAP_USD": "300",
    # ~wider bar windows for fast TFs — Pine fires on signal only (not every bar), so 2.5-bar
    # windows went stale between rsi1h alerts; tier ladder now includes 3m/4m (2026-07-08).
    "PULSE_TV_MTF_CONFIRM_WINDOW_5M_S": "1500",
    "PULSE_TV_MTF_CONFIRM_WINDOW_15M_S": "2250",
    "PULSE_TV_MTF_CONFIRM_WINDOW_30M_S": "4500",
    "PULSE_TV_MTF_CONFIRM_WINDOW_45M_S": "6750",
    "PULSE_TV_MTF_CONFIRM_WINDOW_60M_S": "9000",
    "PULSE_TV_MTF_CONFIRM_WINDOW_240M_S": "36000",
    "PULSE_TV_MTF_CONFIRM_WINDOW_1440M_S": "216000",
    # Legacy 2m + active 3/4m freshness windows (observe-only learning charts).
    "PULSE_TV_MTF_CONFIRM_WINDOW_2M_S": "300",
    "PULSE_TV_MTF_CONFIRM_WINDOW_3M_S": "1200",
    "PULSE_TV_MTF_CONFIRM_WINDOW_4M_S": "1500",
    # Tier 2: selectivity blocks need PF floor + higher min_samples + BH-FDR.
    # 2026-07-05: exploration was 0 (no cold-bucket probes); loosen PF/WR floors for +EV throughput.
    "PULSE_SELECTIVITY_MIN_SAMPLES": "20",
    "PULSE_SELECTIVITY_MIN_PROFIT_FACTOR": "0.85",
    "PULSE_SELECTIVITY_MIN_WIN_RATE": "0.52",
    "PULSE_SELECTIVITY_FDR_Q": "0.10",
    "PULSE_SELECTIVITY_EXPLORATION_RATE": "0.12",
}


def _enforce_context_cohort_coupling(updates: dict) -> dict:
    """Raise PULSE_TV_CONTEXT_MAX_TTC_S if it would deadlock baseline cohort."""
    slugs = [s.strip() for s in updates.get("PULSE_SERIES_SLUGS", "").split(",") if s.strip()]
    rep = evaluate_context_cohort_coupling(
        baseline_cohort_enabled=updates.get("PULSE_BASELINE_COHORT_GATE_ENABLED", "1") == "1",
        tv_context_enabled=updates.get("PULSE_TV_CONTEXT_GATE", "1") == "1",
        configured_context_max_ttc_s=float(updates.get("PULSE_TV_CONTEXT_MAX_TTC_S", "0") or 0),
        cohort_ttc_min_s=float(updates.get("PULSE_BASELINE_COHORT_TTC_MIN_S", "180")),
        cohort_ttc_max_s=float(updates.get("PULSE_BASELINE_COHORT_TTC_MAX_S", "240")),
        window_seconds_list=window_seconds_for_slugs(slugs),
        auto_clamp=False,
    )
    if rep.get("active") and not rep.get("configured_ok"):
        fixed = str(int(rep["required_min_s"]))
        print(
            f"COUPLING: PULSE_TV_CONTEXT_MAX_TTC_S {updates['PULSE_TV_CONTEXT_MAX_TTC_S']} "
            f"-> {fixed} (required for cohort band on {slugs})"
        )
        updates = {**updates, "PULSE_TV_CONTEXT_MAX_TTC_S": fixed}
    return updates


UPDATES = _enforce_context_cohort_coupling(UPDATES)

text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
lines = [ln for ln in text.splitlines() if not ln.strip().startswith("# LOOP ENGINE ARCH")]
seen = set()
out = []
remaining = dict(UPDATES)
for ln in lines:
    if "=" in ln and not ln.lstrip().startswith("#"):
        key = ln.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
            seen.add(key)
        elif key not in seen:
            out.append(ln)
            seen.add(key)
    elif ln.strip():
        out.append(ln)
for key, val in remaining.items():
    out.append(f"{key}={val}")
out.append(
    "# LOOP ENGINE ARCH: directional-only paper lab — 1h up/down + above strike directional lanes"
)
ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"Wrote {ENV_PATH} ({len(UPDATES)} loop-arch keys)")
