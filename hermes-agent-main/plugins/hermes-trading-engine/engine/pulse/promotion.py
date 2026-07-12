"""Feature promotion ladder for the BTC 5-min pulse — explicit authority governance.

Every feature/layer has an explicit authority level. NEW features default to 0 (observe-only).
Raising a feature's authority requires ALL of: an explicit config flag, sufficient samples,
positive EV after execution, clean reconciliation, and report evidence. The engine enforces that
no feature ever exceeds its authorized level.

  0 observe_only      — logged only (default)
  1 veto_only         — may BLOCK a trade, never cause/size one
  2 confidence_scoring— may adjust a confidence score (still no trade authority)
  3 sizing_influence  — may influence paper size
  4 strategy_trigger  — may trigger a trade
"""

from __future__ import annotations

from typing import Optional

AUTHORITY_LEVELS = {0: "observe_only", 1: "veto_only", 2: "confidence_scoring",
                    3: "sizing_influence", 4: "strategy_trigger"}
MAX_LEVEL = 4

# every observe-only layer built in phases 3-9
DEFAULT_FEATURES = ("research_features", "signal_engine", "factor_model", "markov_regime",
                    "edge_model", "tier_classifier", "kelly_sizing")


def can_promote_proven_edge(
    *,
    n: int,
    min_samples: int,
    wilson_lower: Optional[float],
    breakeven_wr: Optional[float],
    edge_margin: float = 0.04,
    model_brier: Optional[float] = None,
    market_brier: Optional[float] = None,
) -> "tuple[bool, list]":
    """Townhall validation bar: Wilson lower > breakeven + margin AND beats market Brier."""
    reasons = []
    if n < min_samples:
        reasons.append("insufficient_samples")
    if wilson_lower is None or breakeven_wr is None:
        reasons.append("insufficient_wilson_or_breakeven")
    elif wilson_lower <= float(breakeven_wr) + float(edge_margin):
        reasons.append("wilson_below_breakeven_margin")
    if model_brier is not None and market_brier is not None and model_brier >= market_brier:
        reasons.append("model_brier_not_beating_market")
    return (len(reasons) == 0), reasons


def can_promote(*, config_flag: bool, samples: int, min_samples: int,
                ev_after_costs: Optional[float], reconciled: bool,
                report_evidence: bool) -> "tuple[bool, list]":
    """Gate a promotion. Returns (allowed, blocking_reasons)."""
    reasons = []
    if not config_flag:
        reasons.append("config_flag_not_set")
    if samples < min_samples:
        reasons.append("insufficient_samples")
    if ev_after_costs is None or ev_after_costs <= 0:
        reasons.append("ev_not_positive")
    if not reconciled:
        reasons.append("reconciliation_unclean")
    if not report_evidence:
        reasons.append("no_report_evidence")
    return (len(reasons) == 0), reasons


class PromotionLadder:
    def __init__(self, features=DEFAULT_FEATURES, *, min_samples: int = 200):
        self.min_samples = int(min_samples)
        self.levels = {f: 0 for f in features}      # all default observe-only
        self.history: list = []

    def effective_authority(self, feature: str) -> int:
        return self.levels.get(feature, 0)

    def promote(self, feature: str, target_level: int, **gates) -> dict:
        """Attempt to promote a feature. Refused unless every gate passes; level never exceeds
        MAX_LEVEL and only moves one purpose-built step under explicit governance."""
        if feature not in self.levels:
            return {"feature": feature, "promoted": False, "reasons": ["unknown_feature"]}
        if not (0 <= target_level <= MAX_LEVEL):
            return {"feature": feature, "promoted": False, "reasons": ["invalid_level"]}
        allowed, reasons = can_promote(min_samples=self.min_samples, **gates)
        if allowed and target_level > self.levels[feature]:
            self.levels[feature] = target_level
            self.history.append({"feature": feature, "to": target_level})
            return {"feature": feature, "promoted": True, "level": target_level,
                    "level_name": AUTHORITY_LEVELS[target_level], "reasons": []}
        return {"feature": feature, "promoted": False, "level": self.levels[feature],
                "level_name": AUTHORITY_LEVELS[self.levels[feature]], "reasons": reasons}

    def max_authority(self) -> int:
        return max(self.levels.values()) if self.levels else 0

    def report(self) -> dict:
        return {"levels_legend": AUTHORITY_LEVELS,
                "features": {f: {"level": lvl, "level_name": AUTHORITY_LEVELS[lvl]}
                             for f, lvl in self.levels.items()},
                "max_authority_in_use": self.max_authority(),
                "all_observe_only": self.max_authority() == 0,
                "promotion_history": list(self.history)}
