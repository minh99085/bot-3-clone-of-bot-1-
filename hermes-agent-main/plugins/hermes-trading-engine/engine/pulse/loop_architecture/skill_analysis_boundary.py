"""Load SKILL_ANALYSIS.md — external cognitive boundary (deterministic, disk-bound)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine.pulse.loop_architecture.asset_triage import TriageConfig

logger = logging.getLogger("pulse.loop_architecture.skill_analysis_boundary")

_TABLE_ROW = re.compile(
    r"^\|\s*`(?P<key>[a-z_]+)`\s*\|\s*(?P<val>[^|]+)\|\s*$",
    re.MULTILINE,
)


@dataclass
class SkillAnalysisBoundary:
    """Parsed SKILL_ANALYSIS.md — bot reads on wake; no in-context guessing."""

    path: Path
    content_hash: str = ""
    loaded: bool = False
    sweet_min: float = 0.47
    sweet_max: float = 0.55
    tail_max: float = 0.10
    min_depth_usd: float = 50.0
    max_slippage_pct: float = 2.0
    min_shares: float = 5.0
    tv_timeframes: tuple[str, ...] = ("5", "15", "30", "60", "240", "1440")
    tail_min_strength: float = 0.55
    wake_count: int = 0

    @classmethod
    def default_paths(cls, data_dir: Optional[Path] = None) -> list[Path]:
        """Search order: data dir copy, container /app, plugin-relative."""
        paths: list[Path] = []
        if data_dir:
            paths.append(Path(data_dir) / "SKILL_ANALYSIS.md")
        env = os.getenv("PULSE_SKILL_ANALYSIS_PATH", "").strip()
        if env:
            paths.append(Path(env))
        paths.extend([
            Path("/app/SKILL_ANALYSIS.md"),
            Path(__file__).resolve().parents[3] / "SKILL_ANALYSIS.md",
        ])
        return paths

    @classmethod
    def load(cls, data_dir: Optional[Path] = None) -> "SkillAnalysisBoundary":
        text = ""
        src: Optional[Path] = None
        for p in cls.default_paths(data_dir):
            if p.is_file():
                try:
                    text = p.read_text(encoding="utf-8")
                    src = p
                    break
                except OSError:
                    continue
        boundary = cls(path=src or Path("SKILL_ANALYSIS.md"))
        if text:
            boundary._parse(text)
            boundary.content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            boundary.loaded = True
            if data_dir:
                boundary._sync_to_data_dir(data_dir, text)
        else:
            logger.warning("SKILL_ANALYSIS.md not found — using coded defaults")
        boundary.wake_count += 1
        return boundary

    def _sync_to_data_dir(self, data_dir: Path, text: str) -> None:
        dest = Path(data_dir) / "SKILL_ANALYSIS.md"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists() or dest.read_text(encoding="utf-8") != text:
                dest.write_text(text, encoding="utf-8")
        except OSError:
            logger.exception("failed to sync SKILL_ANALYSIS.md to %s", dest)

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

    def to_triage_config(self) -> TriageConfig:
        """Merge disk boundary with env overrides (env wins on conflict)."""
        env_cfg = TriageConfig.from_env()
        if os.getenv("PULSE_TRIAGE_SWEET_MIN"):
            return env_cfg
        return TriageConfig(
            sweet_min=self.sweet_min,
            sweet_max=self.sweet_max,
            tail_max=self.tail_max,
            min_depth_usd=self.min_depth_usd,
            max_slippage_pct=self.max_slippage_pct,
            min_shares=self.min_shares,
            tv_timeframes=env_cfg.tv_timeframes,
            tail_min_strength=self.tail_min_strength,
            tv_max_age_s=env_cfg.tv_max_age_s,
        )

    def report(self) -> dict:
        return {
            "loaded": self.loaded,
            "path": str(self.path),
            "content_hash": self.content_hash or None,
            "wake_count": self.wake_count,
            "thresholds": {
                "sweet_min": self.sweet_min,
                "sweet_max": self.sweet_max,
                "tail_max": self.tail_max,
                "min_depth_usd": self.min_depth_usd,
                "max_slippage_pct": self.max_slippage_pct,
                "min_shares": self.min_shares,
                "tv_timeframes": list(self.tv_timeframes),
                "tail_min_strength": self.tail_min_strength,
            },
        }
