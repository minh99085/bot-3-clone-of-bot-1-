"""TradingView DOWN-bias gate (restrict-only, PAPER ONLY).

Townhall P3: the bot's own signal-learning shows bearish_aligned contexts win while
bullish_aligned UP trades lose. This gate blocks proven-losing UP-aligned entries; it can
only make the bot MORE selective and never forces a trade.
"""

from __future__ import annotations

import random
from typing import Optional


class TradingViewDownBiasGate:
    """Restrict-only gate for the asymmetric DOWN/TV edge."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        block_bullish_aligned_up: bool = True,
        block_up_without_bearish: bool = True,
        block_up_on_bearish_down_stack: bool = True,
        block_up_tv_down_non_bearish: bool = True,
        block_up_against_confirmed_down: bool = True,
        block_mixed_mtf_up: bool = True,
        block_bullish_supertrend_up: bool = True,
        block_up_vwap_above: bool = True,
        block_up_bb_expansion_up: bool = True,
        block_up_range_breakout_down: bool = True,
        block_up_range_top: bool = True,
        block_up_bb_squeeze: bool = True,
        block_up_markov_chop_noise: bool = True,
        block_up_htf_bullish: bool = True,
        block_up_bear_close_near_low: bool = True,
        block_up_medium_edge: bool = True,
        block_up_weak_cex: bool = True,
        block_up_late_ttc: bool = True,
        block_up_early_ttc: bool = True,
        block_up_ask_heavy_ob: bool = True,
        block_up_tf_confirm_conflict: bool = True,
        block_up_cvd_neutral: bool = True,
        block_up_cvd_buy_pressure: bool = True,
        block_up_low_conviction: bool = True,
        block_up_bearish_mtf_tv_up: bool = True,
        block_up_mid_ttc: bool = True,
        block_up_neutral_zscore: bool = True,
        block_up_medium_confidence: bool = True,
        block_up_not_stale: bool = True,
        block_up_volume_active: bool = True,
        block_up_underdog_entry: bool = True,
        up_underdog_entry_max: float = 0.55,
        up_late_ttc_min_s: float = 240.0,
        up_early_ttc_max_s: float = 120.0,
        up_mid_ttc_min_s: float = 120.0,
        up_mid_ttc_max_s: float = 180.0,
        up_min_conviction: float = 0.40,
        exploration_rate: float = 0.0,
        seed: Optional[int] = None,
    ):
        self.enabled = bool(enabled)
        self.block_bullish_aligned_up = bool(block_bullish_aligned_up)
        self.block_up_without_bearish = bool(block_up_without_bearish)
        self.block_up_on_bearish_down_stack = bool(block_up_on_bearish_down_stack)
        self.block_up_tv_down_non_bearish = bool(block_up_tv_down_non_bearish)
        self.block_up_against_confirmed_down = bool(block_up_against_confirmed_down)
        self.block_mixed_mtf_up = bool(block_mixed_mtf_up)
        self.block_bullish_supertrend_up = bool(block_bullish_supertrend_up)
        self.block_up_vwap_above = bool(block_up_vwap_above)
        self.block_up_bb_expansion_up = bool(block_up_bb_expansion_up)
        self.block_up_range_breakout_down = bool(block_up_range_breakout_down)
        self.block_up_range_top = bool(block_up_range_top)
        self.block_up_bb_squeeze = bool(block_up_bb_squeeze)
        self.block_up_markov_chop_noise = bool(block_up_markov_chop_noise)
        self.block_up_htf_bullish = bool(block_up_htf_bullish)
        self.block_up_bear_close_near_low = bool(block_up_bear_close_near_low)
        self.block_up_medium_edge = bool(block_up_medium_edge)
        self.block_up_weak_cex = bool(block_up_weak_cex)
        self.block_up_late_ttc = bool(block_up_late_ttc)
        self.block_up_early_ttc = bool(block_up_early_ttc)
        self.block_up_ask_heavy_ob = bool(block_up_ask_heavy_ob)
        self.block_up_tf_confirm_conflict = bool(block_up_tf_confirm_conflict)
        self.block_up_cvd_neutral = bool(block_up_cvd_neutral)
        self.block_up_cvd_buy_pressure = bool(block_up_cvd_buy_pressure)
        self.block_up_low_conviction = bool(block_up_low_conviction)
        self.block_up_bearish_mtf_tv_up = bool(block_up_bearish_mtf_tv_up)
        self.block_up_mid_ttc = bool(block_up_mid_ttc)
        self.block_up_neutral_zscore = bool(block_up_neutral_zscore)
        self.block_up_medium_confidence = bool(block_up_medium_confidence)
        self.block_up_not_stale = bool(block_up_not_stale)
        self.block_up_volume_active = bool(block_up_volume_active)
        self.block_up_underdog_entry = bool(block_up_underdog_entry)
        self.up_underdog_entry_max = max(0.0, min(1.0, float(up_underdog_entry_max)))
        self.up_late_ttc_min_s = max(0.0, float(up_late_ttc_min_s))
        self.up_early_ttc_max_s = max(0.0, float(up_early_ttc_max_s))
        self.up_mid_ttc_min_s = max(0.0, float(up_mid_ttc_min_s))
        self.up_mid_ttc_max_s = max(0.0, float(up_mid_ttc_max_s))
        self.up_min_conviction = max(0.0, min(1.0, float(up_min_conviction)))
        self.exploration_rate = max(0.0, min(0.05, float(exploration_rate)))
        self.passed = 0
        self.blocked = 0
        self.explored = 0
        self.block_reasons: dict = {}
        self.explore_reasons: dict = {}
        self._rng = random.Random(seed)

    def violations(
        self,
        *,
        side: Optional[str],
        mtf_alignment=None,
        tv_direction=None,
        tf_confirm=None,
        supertrend_direction=None,
        vwap_state=None,
        bb_state=None,
        range_state=None,
        markov_state=None,
        htf_bias=None,
        candle_pressure=None,
        edge_score_bucket=None,
        cex_agreement_bucket=None,
        ob_pressure_bucket=None,
        cvd_state=None,
        conviction=None,
        ttc_s=None,
        zscore_bucket=None,
        confidence_tier=None,
        stale_divergence=None,
        volume_state=None,
        ask_price=None,
    ) -> list[str]:
        if not side or str(side).lower() != "up":
            return []
        reasons = []
        ma = str(mtf_alignment or "").strip().lower()
        td = str(tv_direction or "").strip().upper()
        tc = str(tf_confirm or "").strip().lower()
        st = str(supertrend_direction or "").strip().lower()
        vw = str(vwap_state or "").strip().lower()
        bb = str(bb_state or "").strip().lower()
        rs = str(range_state or "").strip().lower()
        ms = str(markov_state or "").strip().lower()
        hb = str(htf_bias or "").strip().lower()
        cp = str(candle_pressure or "").strip().lower()
        esb = str(edge_score_bucket or "").strip().lower()
        cex = str(cex_agreement_bucket or "").strip().lower()
        ob = str(ob_pressure_bucket or "").strip().lower()
        cvd = str(cvd_state or "").strip().lower()
        zb = str(zscore_bucket or "").strip().lower()
        ct = str(confidence_tier or "").strip().lower()
        sd = str(stale_divergence or "").strip().lower()
        vs = str(volume_state or "").strip().lower()
        if self.block_up_not_stale and sd == "not_stale":
            reasons.append("tv_down_bias_up_not_stale")
        if self.block_bullish_aligned_up and ma == "bullish_aligned":
            reasons.append("tv_down_bias_bullish_aligned_up")
        if self.block_mixed_mtf_up and ma == "mixed":
            reasons.append("tv_down_bias_mixed_mtf_up")
        if self.block_bullish_supertrend_up and st == "bullish":
            reasons.append("tv_down_bias_bullish_supertrend_up")
        if self.block_up_without_bearish and td == "UP" and ma != "bearish_aligned":
            reasons.append("tv_down_bias_up_without_bearish")
        if self.block_up_on_bearish_down_stack and ma == "bearish_aligned" and td == "DOWN":
            reasons.append("tv_down_bias_up_on_bearish_down_stack")
        if (self.block_up_tv_down_non_bearish and td == "DOWN"
                and ma not in ("bearish_aligned",)):
            reasons.append("tv_down_bias_up_tv_down_non_bearish")
        if self.block_up_against_confirmed_down and tc == "confirmed_down":
            reasons.append("tv_down_bias_up_against_confirmed_down")
        if self.block_up_bearish_mtf_tv_up and ma == "bearish_aligned" and td == "UP":
            reasons.append("tv_down_bias_up_bearish_mtf_tv_up")
        if self.block_up_tf_confirm_conflict and tc == "conflict":
            reasons.append("tv_down_bias_up_tf_confirm_conflict")
        if self.block_up_vwap_above and vw == "above":
            reasons.append("tv_down_bias_up_vwap_above")
        if self.block_up_bb_expansion_up and bb == "expansion_up":
            reasons.append("tv_down_bias_up_bb_expansion_up")
        if self.block_up_range_breakout_down and rs == "breakout_down":
            reasons.append("tv_down_bias_up_range_breakout_down")
        if self.block_up_range_top and rs == "range_top":
            reasons.append("tv_down_bias_up_range_top")
        if self.block_up_bb_squeeze and bb == "squeeze":
            reasons.append("tv_down_bias_up_bb_squeeze")
        if self.block_up_markov_chop_noise and ms == "chop_noise":
            reasons.append("tv_down_bias_up_markov_chop_noise")
        if self.block_up_volume_active and vs == "active":
            reasons.append("tv_down_bias_up_volume_active")
        if self.block_up_underdog_entry and ask_price is not None:
            try:
                if float(ask_price) < self.up_underdog_entry_max:
                    reasons.append("tv_down_bias_up_underdog_entry")
            except (TypeError, ValueError):
                pass
        if self.block_up_htf_bullish and hb == "bullish":
            reasons.append("tv_down_bias_up_htf_bullish")
        if self.block_up_bear_close_near_low and cp == "bear_close_near_low":
            reasons.append("tv_down_bias_up_bear_close_near_low")
        if self.block_up_medium_edge and esb not in ("high", "very_high"):
            reasons.append("tv_down_bias_up_medium_edge")
        if self.block_up_weak_cex and cex != "strong":
            reasons.append("tv_down_bias_up_weak_cex")
        if self.block_up_ask_heavy_ob and ob == "ask_heavy":
            reasons.append("tv_down_bias_up_ask_heavy_ob")
        if self.block_up_cvd_neutral and cvd == "neutral":
            reasons.append("tv_down_bias_up_cvd_neutral")
        if self.block_up_cvd_buy_pressure and cvd == "buy_pressure":
            reasons.append("tv_down_bias_up_cvd_buy_pressure")
        if self.block_up_neutral_zscore and zb == "-1..1":
            reasons.append("tv_down_bias_up_neutral_zscore")
        if self.block_up_medium_confidence and ct == "medium":
            reasons.append("tv_down_bias_up_medium_confidence")
        if self.block_up_low_conviction and conviction is not None:
            if float(conviction) < self.up_min_conviction:
                reasons.append("tv_down_bias_up_low_conviction")
        if ttc_s is not None:
            ttc = float(ttc_s)
            if self.block_up_late_ttc and ttc >= self.up_late_ttc_min_s:
                reasons.append("tv_down_bias_up_late_ttc")
            if self.block_up_early_ttc and ttc < self.up_early_ttc_max_s:
                reasons.append("tv_down_bias_up_early_ttc")
            if (self.block_up_mid_ttc
                    and ttc >= self.up_mid_ttc_min_s
                    and ttc < self.up_mid_ttc_max_s):
                reasons.append("tv_down_bias_up_mid_ttc")
        return reasons

    def evaluate(
        self,
        *,
        side: Optional[str],
        mtf_alignment=None,
        tv_direction=None,
        tf_confirm=None,
        supertrend_direction=None,
        vwap_state=None,
        bb_state=None,
        range_state=None,
        markov_state=None,
        htf_bias=None,
        candle_pressure=None,
        edge_score_bucket=None,
        cex_agreement_bucket=None,
        ob_pressure_bucket=None,
        cvd_state=None,
        conviction=None,
        ttc_s=None,
        zscore_bucket=None,
        confidence_tier=None,
        stale_divergence=None,
        volume_state=None,
        ask_price=None,
    ) -> dict:
        if not self.enabled:
            return {"decision": "pass", "reasons": [], "active": False}
        reasons = self.violations(side=side, mtf_alignment=mtf_alignment,
                                  tv_direction=tv_direction, tf_confirm=tf_confirm,
                                  supertrend_direction=supertrend_direction,
                                  vwap_state=vwap_state, bb_state=bb_state,
                                  range_state=range_state, markov_state=markov_state,
                                  htf_bias=htf_bias, candle_pressure=candle_pressure,
                                  edge_score_bucket=edge_score_bucket,
                                  cex_agreement_bucket=cex_agreement_bucket,
                                  ob_pressure_bucket=ob_pressure_bucket,
                                  cvd_state=cvd_state, conviction=conviction,
                                  ttc_s=ttc_s, zscore_bucket=zscore_bucket,
                                  confidence_tier=confidence_tier,
                                  stale_divergence=stale_divergence,
                                  volume_state=volume_state,
                                  ask_price=ask_price)
        if not reasons:
            self.passed += 1
            return {"decision": "pass", "reasons": [], "active": True}
        if self.exploration_rate > 0 and self._rng.random() < self.exploration_rate:
            self.explored += 1
            for r in reasons:
                self.explore_reasons[r] = self.explore_reasons.get(r, 0) + 1
            return {"decision": "explore", "reasons": reasons, "active": True}
        self.blocked += 1
        for r in reasons:
            self.block_reasons[r] = self.block_reasons.get(r, 0) + 1
        return {"decision": "block", "reasons": reasons, "active": True}

    def report(self) -> dict:
        return {
            "enabled": self.enabled,
            "block_bullish_aligned_up": self.block_bullish_aligned_up,
            "block_up_without_bearish": self.block_up_without_bearish,
            "block_up_on_bearish_down_stack": self.block_up_on_bearish_down_stack,
            "block_up_tv_down_non_bearish": self.block_up_tv_down_non_bearish,
            "block_up_against_confirmed_down": self.block_up_against_confirmed_down,
            "block_mixed_mtf_up": self.block_mixed_mtf_up,
            "block_bullish_supertrend_up": self.block_bullish_supertrend_up,
            "block_up_vwap_above": self.block_up_vwap_above,
            "block_up_bb_expansion_up": self.block_up_bb_expansion_up,
            "block_up_range_breakout_down": self.block_up_range_breakout_down,
            "block_up_range_top": self.block_up_range_top,
            "block_up_bb_squeeze": self.block_up_bb_squeeze,
            "block_up_markov_chop_noise": self.block_up_markov_chop_noise,
            "block_up_htf_bullish": self.block_up_htf_bullish,
            "block_up_bear_close_near_low": self.block_up_bear_close_near_low,
            "block_up_medium_edge": self.block_up_medium_edge,
            "block_up_weak_cex": self.block_up_weak_cex,
            "block_up_late_ttc": self.block_up_late_ttc,
            "block_up_early_ttc": self.block_up_early_ttc,
            "block_up_ask_heavy_ob": self.block_up_ask_heavy_ob,
            "block_up_tf_confirm_conflict": self.block_up_tf_confirm_conflict,
            "block_up_cvd_neutral": self.block_up_cvd_neutral,
            "block_up_cvd_buy_pressure": self.block_up_cvd_buy_pressure,
            "block_up_low_conviction": self.block_up_low_conviction,
            "block_up_bearish_mtf_tv_up": self.block_up_bearish_mtf_tv_up,
            "block_up_mid_ttc": self.block_up_mid_ttc,
            "block_up_neutral_zscore": self.block_up_neutral_zscore,
            "block_up_medium_confidence": self.block_up_medium_confidence,
            "block_up_not_stale": self.block_up_not_stale,
            "block_up_volume_active": self.block_up_volume_active,
            "block_up_underdog_entry": self.block_up_underdog_entry,
            "up_underdog_entry_max": self.up_underdog_entry_max,
            "up_late_ttc_min_s": self.up_late_ttc_min_s,
            "up_early_ttc_max_s": self.up_early_ttc_max_s,
            "up_mid_ttc_min_s": self.up_mid_ttc_min_s,
            "up_mid_ttc_max_s": self.up_mid_ttc_max_s,
            "up_min_conviction": self.up_min_conviction,
            "exploration_rate": self.exploration_rate,
            "passed": self.passed,
            "blocked": self.blocked,
            "explored": self.explored,
            "block_reasons": dict(self.block_reasons),
            "explore_reasons": dict(self.explore_reasons),
            "note": "restrict-only: harvest DOWN/TV asymmetry by blocking proven-losing UP stacks",
        }

    def to_state(self) -> dict:
        return {"passed": self.passed, "blocked": self.blocked, "explored": self.explored,
                "block_reasons": dict(self.block_reasons),
                "explore_reasons": dict(self.explore_reasons)}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.passed = int(data.get("passed", 0) or 0)
        self.blocked = int(data.get("blocked", 0) or 0)
        self.explored = int(data.get("explored", 0) or 0)
        self.block_reasons = {k: int(v or 0) for k, v in (data.get("block_reasons") or {}).items()}
        self.explore_reasons = {k: int(v or 0)
                                for k, v in (data.get("explore_reasons") or {}).items()}