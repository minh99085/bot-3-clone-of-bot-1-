"""Standalone SKILL_ANALYSIS.md parser for cloud scripts (stdlib only)."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

_TABLE_ROW = re.compile(
    r"^\|\s*`(?P<key>[a-z_]+)`\s*\|\s*(?P<val>[^|]+)\|\s*$",
    re.MULTILINE,
)


@dataclass
class SkillThresholds:
    """Parsed operational thresholds from SKILL_ANALYSIS.md."""

    path: Path
    loaded: bool = False
    content_hash: str = ""
    sweet_min: float = 0.47
    sweet_max: float = 0.55
    tail_max: float = 0.10
    min_depth_usd: float = 50.0
    max_slippage_pct: float = 2.0
    min_shares: float = 5.0
    tv_timeframes: tuple[str, ...] = ("15", "30", "45", "55")
    tail_min_strength: float = 0.55

    @classmethod
    def search_paths(cls, repo_root: Path) -> list[Path]:
        paths: list[Path] = []
        env = os.getenv("PULSE_SKILL_ANALYSIS_PATH", "").strip()
        if env:
            paths.append(Path(env))
        paths.extend([
            repo_root / "SKILL_ANALYSIS.md",
            repo_root / "hermes-agent-main" / "plugins" / "hermes-trading-engine" / "SKILL_ANALYSIS.md",
        ])
        return paths

    @classmethod
    def load(cls, repo_root: Path | None = None) -> "SkillThresholds":
        root = repo_root or Path(__file__).resolve().parents[1]
        text = ""
        src: Path | None = None
        for p in cls.search_paths(root):
            if p.is_file():
                text = p.read_text(encoding="utf-8")
                src = p
                break
        obj = cls(path=src or (root / "SKILL_ANALYSIS.md"))
        if text:
            obj._parse(text)
            obj.content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            obj.loaded = True
        return obj

    def _parse(self, text: str) -> None:
        for m in _TABLE_ROW.finditer(text):
            key = m.group("key").strip()
            raw = m.group("val").strip()
            if key == "sweet_min":
                self.sweet_min = float(raw)
            elif key == "sweet_max":
                self.sweet_max = float(raw)
            elif key == "tail_max":
                self.tail_max = float(raw)
            elif key == "min_depth_usd":
                self.min_depth_usd = float(raw)
            elif key == "max_slippage_pct":
                self.max_slippage_pct = float(raw)
            elif key == "min_shares":
                self.min_shares = float(raw)
            elif key == "tail_min_strength":
                self.tail_min_strength = float(raw)
            elif key == "tv_timeframes":
                self.tv_timeframes = tuple(t.strip() for t in raw.split(",") if t.strip())

    def classify_ask(self, ask: float, *, tail_breakthrough: bool = False) -> str:
        """Skill verification protocol status codes."""
        if self.sweet_min <= ask <= self.sweet_max:
            return "PROCEED_SWEEP"
        if ask < self.tail_max and tail_breakthrough:
            return "PROCEED_10X"
        if ask < self.tail_max:
            return "REJECT_NO_BREAKTHROUGH"
        return "REJECT_PRICE_OUT_OF_BAND"

    def as_dict(self) -> dict:
        return {
            "loaded": self.loaded,
            "path": str(self.path),
            "content_hash": self.content_hash or None,
            "sweet_min": self.sweet_min,
            "sweet_max": self.sweet_max,
            "tail_max": self.tail_max,
            "min_depth_usd": self.min_depth_usd,
            "max_slippage_pct": self.max_slippage_pct,
            "min_shares": self.min_shares,
            "tv_timeframes": list(self.tv_timeframes),
            "tail_min_strength": self.tail_min_strength,
        }
