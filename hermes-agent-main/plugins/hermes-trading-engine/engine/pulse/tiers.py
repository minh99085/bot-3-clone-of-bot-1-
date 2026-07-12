"""Selective win-rate tier classifier for the BTC 5-min pulse (REPORT-ONLY).

Labels each historical bucket so we can SEE high-confidence pockets without overtrading:
  * Tier A+  : historically strongest — win-rate >= 80%, positive PnL/EV, large clean sample,
               reconciled, acceptable drawdown, no safety violation
  * Tier A   : strong but still validating (>=70%, positive, moderate sample)
  * Tier B   : observe-only (default / insufficient evidence)
  * Tier C   : reject (losing / sub-coin-flip)
  * Tier D   : dangerous / never trade (safety violation)

REPORT-ONLY: this produces a table; it does not trade or veto. (A future phase may grant
veto-only authority behind a config flag — see the promotion ladder.)
"""

from __future__ import annotations

from typing import Optional

TIERS = ("A+", "A", "B", "C", "D")


def classify_tier(*, n: int, win_rate: Optional[float], pnl_usd: Optional[float],
                  reconciled: bool = True, safety_ok: bool = True,
                  max_drawdown: Optional[float] = None, drawdown_limit: float = 0.5,
                  min_n_aplus: int = 100, min_n_a: int = 30,
                  aplus_win: float = 0.80, a_win: float = 0.70) -> dict:
    """Return {tier, reasons} for a bucket's historical stats. Report-only."""
    reasons = []
    if not safety_ok:
        return {"tier": "D", "reasons": ["safety_violation"]}
    if max_drawdown is not None and max_drawdown > drawdown_limit:
        return {"tier": "D", "reasons": ["excessive_drawdown"]}
    if not n or win_rate is None:
        return {"tier": "B", "reasons": ["insufficient_samples"]}
    if (pnl_usd is not None and pnl_usd < 0) or win_rate < 0.5:
        return {"tier": "C", "reasons": ["losing_or_subcoinflip"]}
    pos_pnl = (pnl_usd is None) or (pnl_usd > 0)
    if win_rate >= aplus_win and pos_pnl and n >= min_n_aplus and reconciled:
        return {"tier": "A+", "reasons": ["winrate_ge_80", "positive_pnl", "large_clean_sample"]}
    if win_rate >= a_win and pos_pnl and n >= min_n_a:
        reasons.append("strong_validating")
        if n < min_n_aplus:
            reasons.append("sample_below_aplus")
        return {"tier": "A", "reasons": reasons}
    return {"tier": "B", "reasons": ["below_tier_a_threshold"]}


def build_tier_table(grouped: dict, *, dimension: str, reconciled: bool = True,
                     safety_ok: bool = True, **kw) -> dict:
    """Map a grouped-PnL dict {bucket: {n, win_rate, pnl_usd}} -> {bucket: {tier, reasons, ...}}."""
    out = {}
    for bucket, g in (grouped or {}).items():
        res = classify_tier(n=int(g.get("n", 0) or 0), win_rate=g.get("win_rate"),
                            pnl_usd=g.get("pnl_usd"), reconciled=reconciled,
                            safety_ok=safety_ok, **kw)
        out[bucket] = {"dimension": dimension, "tier": res["tier"], "reasons": res["reasons"],
                       "n": g.get("n"), "win_rate": g.get("win_rate"), "pnl_usd": g.get("pnl_usd")}
    return out


def tier_report(grouped_dimensions: dict, *, reconciled: bool = True,
                safety_ok: bool = True) -> dict:
    """Build the full report-only tier table across multiple bucket dimensions + a tier census."""
    table = {}
    census = {t: 0 for t in TIERS}
    for dim, grouped in (grouped_dimensions or {}).items():
        t = build_tier_table(grouped, dimension=dim, reconciled=reconciled, safety_ok=safety_ok)
        for bucket, row in t.items():
            table[f"{dim}:{bucket}"] = row
            census[row["tier"]] = census.get(row["tier"], 0) + 1
    return {"report_only": True, "affects_trading": False, "tier_census": census,
            "table": table}
