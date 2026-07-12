"""Binary Intel controller — invented bot capability for Polymarket binaries.

Wires:
  * math_core (digital option formulas)
  * tv_universal (5m RSI for all lanes/symbols)
  * pre_trade script (before fill)
  * post_trade learner (after settlement)
  * grok_protocol (structured Grok compute)

Enable: PULSE_BINARY_INTEL_ENABLED=1
"""

from __future__ import annotations

from typing import Optional

from engine.pulse.binary_intel.grok_protocol import (
    build_post_trade_grok_payload,
    build_pre_trade_grok_payload,
    should_request_pre_trade_grok,
)
from engine.pulse.binary_intel.post_trade import BinaryIntelLearner
from engine.pulse.binary_intel.pre_trade import run_pre_trade_intel


class BinaryIntelController:
    def __init__(
        self,
        *,
        enabled: bool = True,
        grok_compute_enabled: bool = True,
        max_age_s: float = 2700.0,
        kelly_fraction: float = 0.25,
        aligned_mult: float = 1.15,
        opposed_mult: float = 0.45,
        min_intel_score: float = 0.28,
        exploration_rate: float = 0.05,
        min_size_scale: float = 0.40,
    ):
        self.enabled = bool(enabled)
        self.grok_compute_enabled = bool(grok_compute_enabled)
        self.max_age_s = float(max_age_s)
        self.kelly_fraction = float(kelly_fraction)
        self.aligned_mult = float(aligned_mult)
        self.opposed_mult = float(opposed_mult)
        self.min_intel_score = float(min_intel_score)
        self.exploration_rate = float(exploration_rate)
        self.min_size_scale = float(min_size_scale)
        self.learner = BinaryIntelLearner(enabled=self.enabled)
        self._last_pre: Optional[dict] = None
        self._last_grok_pre: Optional[dict] = None
        self._last_grok_post: Optional[dict] = None

    def analyze_pre_trade(
        self,
        *,
        intake=None,
        window=None,
        s_now=None,
        s_open=None,
        sigma_per_sec=None,
        ttc_s: float = 0.0,
        window_seconds: float = 900.0,
        poly_mid=None,
        model_p_up=None,
        proposed_side=None,
        ask=None,
        now: float,
        readiness_score=None,
        p_uncertainty: float = 0.0,
        bundle_excerpt=None,
    ) -> Optional[dict]:
        if not self.enabled:
            return None
        result = run_pre_trade_intel(
            intake=intake,
            window=window,
            s_now=s_now,
            s_open=s_open,
            sigma_per_sec=sigma_per_sec,
            ttc_s=ttc_s,
            window_seconds=window_seconds,
            poly_mid=poly_mid,
            model_p_up=model_p_up,
            proposed_side=proposed_side,
            ask=ask,
            now=now,
            readiness_score=readiness_score,
            p_uncertainty=p_uncertainty,
            max_age_s=self.max_age_s,
            kelly_fraction=self.kelly_fraction,
            aligned_mult=self.aligned_mult,
            opposed_mult=self.opposed_mult,
            min_intel_score=self.min_intel_score,
            exploration_rate=self.exploration_rate,
            min_size_scale=self.min_size_scale,
        )
        # Apply learned blend weights if available
        w = self.learner._weights
        intel = float(result.get("intelligence_score") or 0.5)
        ready = float(readiness_score) if readiness_score is not None else 0.55
        tv_dec = ((result.get("tv_universal") or {}).get("decision") or {}).get("decision")
        tv_confirm = 1.0 if tv_dec == "confirm" else (0.35 if tv_dec == "fade" else 0.55)
        composite = (w["intel"] * intel + w["readiness"] * ready + w["tv_confirm"] * tv_confirm)
        result["composite_score"] = round(composite, 4)
        result["research_tags"]["binary_intel_score"] = round(composite, 4)
        result["learned_weights"] = dict(w)

        self._last_pre = result
        if should_request_pre_trade_grok(result, enabled=self.grok_compute_enabled):
            self._last_grok_pre = build_pre_trade_grok_payload(
                binary_intel=result, bundle_excerpt=bundle_excerpt)
            result["grok_compute"] = self._last_grok_pre
        else:
            result["grok_compute"] = None
        return result

    def record_settled(
        self,
        *,
        won: bool,
        pnl_usd: float,
        side=None,
        asset: str = "btc",
        lane: str = "15m",
        research: Optional[dict] = None,
        now: Optional[float] = None,
        lessons_book=None,
    ) -> Optional[dict]:
        if not self.enabled:
            return None
        rt = research or {}
        row = self.learner.record_settled(
            won=won,
            pnl_usd=pnl_usd,
            side=side,
            asset=asset,
            lane=lane,
            intel_score=rt.get("binary_intel_intelligence"),
            composite_score=rt.get("binary_intel_score"),
            rsi_lean=rt.get("binary_intel_rsi_lean") or rt.get("tv_rsi_overlay_lean"),
            rsi_aligned=rt.get("tv_rsi_overlay_aligned"),
            rsi_decision=rt.get("binary_intel_rsi_decision"),
            displacement_z=rt.get("binary_intel_z"),
            now=now,
        )
        adj = self.learner.maybe_adjust(now=now)
        autopsy = self.learner.grok_autopsy_brief(row or {}, won=won)
        self._last_grok_post = build_post_trade_grok_payload(autopsy=autopsy)

        if lessons_book is not None:
            for kind, key, rule in self.learner.lessons_for_book():
                try:
                    lessons_book.add(kind=kind, key=key, rule=rule, now=now)
                except Exception:  # noqa: BLE001
                    pass

        return {
            "row": row,
            "adjustment": adj,
            "grok_autopsy": self._last_grok_post,
            "learner": self.learner.report(),
        }

    def size_mult(self, pre: Optional[dict]) -> float:
        if not pre:
            return 1.0
        try:
            return float(pre.get("size_mult") or 1.0)
        except (TypeError, ValueError):
            return 1.0

    def hard_block(self, pre: Optional[dict]) -> bool:
        return bool(pre and pre.get("hard_block"))

    def report(self) -> dict:
        return {
            "enabled": self.enabled,
            "grok_compute_enabled": self.grok_compute_enabled,
            "learner": self.learner.report(),
            "last_pre_score": ((self._last_pre or {}).get("composite_score")
                               if self._last_pre else None),
            "last_grok_pre_tier": ((self._last_grok_pre or {}).get("compute_tier")
                                  if self._last_grok_pre else None),
        }

    def to_state(self) -> dict:
        return {"learner": self.learner.to_state(), "enabled": self.enabled}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.learner.load_state(data.get("learner") or {})
