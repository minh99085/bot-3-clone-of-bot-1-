"""DOWN microstructure stack grader (observe-only, Townhall P3/P2).

Grades the composite: bearish_aligned + stale_polymarket_down + ttc in [90,240]s.
Promotion requires Wilson lower > breakeven + edge margin and is NEVER auto-applied.
"""

from __future__ import annotations

from typing import Optional

from engine.pulse.cex_lead import _wilson_lower
from engine.pulse.selectivity import breakeven_win_rate


def classify_down_stack(
    *,
    mtf_alignment=None,
    stale_divergence=None,
    ttc_s: Optional[float] = None,
) -> str:
    ma = str(mtf_alignment or "").strip().lower()
    sd = str(stale_divergence or "").strip().lower()
    ttc = float(ttc_s) if ttc_s is not None else None
    if ma == "bearish_aligned" and sd == "stale_polymarket_down":
        if ttc is not None and 90.0 <= ttc <= 240.0:
            return "bearish_stale_late"
        return "bearish_stale"
    if ma == "bearish_aligned":
        return "bearish_only"
    if sd == "stale_polymarket_down":
        return "stale_down_only"
    return "other"


class DownStackGrader:
    """Observe-only bucket stats for DOWN stack composites."""

    def __init__(self, *, min_samples: int = 30, edge_margin: float = 0.04, confidence_z: float = 1.64):
        self.min_samples = int(min_samples)
        self.edge_margin = float(edge_margin)
        self.confidence_z = float(confidence_z)
        self.buckets: dict = {}

    def record(self, *, bucket: str, won: bool, pnl: float, entry_price: Optional[float]) -> None:
        b = self.buckets.setdefault(
            bucket,
            {"n": 0, "wins": 0, "pnl": 0.0, "entry_sum": 0.0, "entry_n": 0},
        )
        b["n"] += 1
        b["wins"] += int(bool(won))
        b["pnl"] = round(b["pnl"] + float(pnl), 6)
        if entry_price is not None:
            b["entry_sum"] += float(entry_price)
            b["entry_n"] += 1

    def _bucket_row(self, bucket: str, st: dict) -> dict:
        n = int(st["n"])
        wins = int(st["wins"])
        wr = wins / n if n else None
        avg_entry = (st["entry_sum"] / st["entry_n"]) if st.get("entry_n") else None
        wl = _wilson_lower(wins, n, self.confidence_z) if n else None
        be = breakeven_win_rate(1.0, 1.0) if avg_entry is None else avg_entry
        if avg_entry is not None:
            be = float(avg_entry)
        proven = (
            n >= self.min_samples
            and wl is not None
            and avg_entry is not None
            and wl > be + self.edge_margin
            and st["pnl"] > 0
        )
        return {
            "bucket": bucket,
            "n": n,
            "win_rate": (round(wr, 4) if wr is not None else None),
            "wilson_lower": (round(wl, 4) if wl is not None else None),
            "avg_entry": (round(avg_entry, 4) if avg_entry is not None else None),
            "breakeven_wr": round(be, 4),
            "pnl_usd": round(st["pnl"], 4),
            "proven": proven,
        }

    def report(self) -> dict:
        rows = [self._bucket_row(k, v) for k, v in sorted(self.buckets.items())]
        proven = [r["bucket"] for r in rows if r.get("proven")]
        return {
            "observe_only": True,
            "affects_trading": False,
            "min_samples": self.min_samples,
            "edge_margin": self.edge_margin,
            "buckets": rows,
            "any_proven": bool(proven),
            "proven_buckets": proven,
            "promotion_rule": (
                f"n>={self.min_samples} AND wilson_lower>avg_entry+{self.edge_margin} AND pnl>0"
            ),
        }

    def to_state(self) -> dict:
        return {"buckets": self.buckets, "min_samples": self.min_samples,
                "edge_margin": self.edge_margin, "confidence_z": self.confidence_z}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.buckets = dict(data.get("buckets") or {})
        self.min_samples = int(data.get("min_samples", self.min_samples) or self.min_samples)
        self.edge_margin = float(data.get("edge_margin", self.edge_margin) or self.edge_margin)
        self.confidence_z = float(data.get("confidence_z", self.confidence_z) or self.confidence_z)