"""Risk-Guardian Meta-Controller (RGMC).

Separate agent watching rolling WR / DD / concentration.
Actions (tighten-only — NEVER loosen frozen gates):
  - lower soft_kappa_scale
  - lower size_multiplier
  - disable weak MCHB families
  - append audit to LESSONS.md + STATE.md

Rollback signal if live WR < 78% after promote.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from autonomy.freeze import ROLLBACK_WR_FLOOR, soft_kappa
from autonomy.schemas import AutonomyState
from hermes.state_io import (
    KNOWLEDGE,
    append_jsonl,
    paper_dir,
    read_state_md,
    update_state_field,
    write_text,
)

logger = logging.getLogger(__name__)


def _audit_path():
    return paper_dir() / "rgmc_audit.jsonl"


class RiskGuardian:
    def __init__(self, state: Optional[AutonomyState] = None) -> None:
        self.state = state or AutonomyState()

    def observe_settlement(
        self,
        *,
        won: bool,
        pnl_usd: float,
        equity: Optional[float] = None,
    ) -> list[str]:
        st = self.state
        st.n_resolved += 1
        st.rolling_n += 1
        if won:
            st.rolling_wins += 1
        # Rolling window ~40
        if st.rolling_n > 40:
            # Decay roughly: keep ratio, shrink counts
            st.rolling_wins = max(0, st.rolling_wins - 1 if not won else st.rolling_wins)
            # Simpler: reset half
            if st.rolling_n > 50:
                st.rolling_wins = int(st.rolling_wins * 0.8)
                st.rolling_n = int(st.rolling_n * 0.8)
        if equity is not None:
            st.equity = float(equity)
            st.peak_equity = max(st.peak_equity, st.equity)
        st.updated_at = datetime.now(timezone.utc)
        return self.evaluate()

    def rolling_wr(self) -> float:
        if self.state.rolling_n <= 0:
            return 1.0
        return self.state.rolling_wins / self.state.rolling_n

    def drawdown(self) -> float:
        if self.state.peak_equity <= 0:
            return 0.0
        return max(0.0, 1.0 - self.state.equity / self.state.peak_equity)

    def evaluate(self) -> list[str]:
        """Apply tighten-only actions; return list of action strings."""
        actions: list[str] = []
        wr = self.rolling_wr()
        dd = self.drawdown()
        st = self.state

        if st.rolling_n >= 15 and wr < 0.78:
            new_scale = max(0.40, st.soft_kappa_scale * 0.85)
            if new_scale < st.soft_kappa_scale - 1e-9:
                st.soft_kappa_scale = new_scale
                actions.append(f"tighten soft_kappa_scale→{new_scale:.2f} (WR={wr:.1%})")
            new_sz = max(0.35, st.size_multiplier * 0.85)
            if new_sz < st.size_multiplier - 1e-9:
                st.size_multiplier = new_sz
                actions.append(f"tighten size_multiplier→{new_sz:.2f}")

        if dd >= 0.08:
            st.soft_kappa_scale = min(st.soft_kappa_scale, 0.55)
            st.size_multiplier = min(st.size_multiplier, 0.50)
            actions.append(f"DD guard soft_kappa={st.soft_kappa_scale:.2f} size={st.size_multiplier:.2f}")

        if st.rolling_n >= 25 and wr < ROLLBACK_WR_FLOOR:
            actions.append(f"ROLLBACK_SIGNAL wr={wr:.1%}<{ROLLBACK_WR_FLOOR:.0%}")

        # Recovery: slowly restore soft knobs when healthy (still ≤ 1.0, never above freeze)
        if st.rolling_n >= 20 and wr >= 0.85 and dd < 0.04:
            if st.soft_kappa_scale < 1.0:
                st.soft_kappa_scale = min(1.0, st.soft_kappa_scale + 0.02)
                actions.append(f"soft_recover kappa_scale→{st.soft_kappa_scale:.2f}")
            if st.size_multiplier < 1.0:
                st.size_multiplier = min(1.0, st.size_multiplier + 0.02)
                actions.append(f"soft_recover size_multiplier→{st.size_multiplier:.2f}")

        for a in actions:
            self._audit(a)
        return actions

    def disable_family(self, family: str, reason: str) -> None:
        if family not in self.state.disabled_families:
            self.state.disabled_families.append(family)
        msg = f"disable_family {family}: {reason}"
        self._audit(msg)
        try:
            from autonomy.mchb import get_mchb

            get_mchb().disable_family(family)
        except Exception as exc:  # noqa: BLE001
            logger.debug("mchb disable failed: %s", exc)

    def _audit(self, action: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "rolling_wr": self.rolling_wr(),
            "dd": self.drawdown(),
            "n": self.state.n_resolved,
            "soft_kappa_scale": self.state.soft_kappa_scale,
            "size_multiplier": self.state.size_multiplier,
        }
        self.state.audit = (self.state.audit + [entry])[-200:]
        try:
            append_jsonl(_audit_path(), entry)
        except Exception as exc:  # noqa: BLE001
            logger.debug("rgmc audit jsonl: %s", exc)
        self._append_lessons(action)
        self._update_state_md()

    def _append_lessons(self, action: str) -> None:
        path = KNOWLEDGE / "LESSONS.md"
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            block = (
                f"\n### RGMC {stamp}\n"
                f"- **Action**: {action}\n"
                f"- **Rolling WR**: {self.rolling_wr():.1%} (n={self.state.rolling_n})\n"
                f"- **DD**: {self.drawdown():.1%}\n"
                f"- **soft_kappa_scale**: {self.state.soft_kappa_scale:.2f}\n"
                f"- **size_multiplier**: {self.state.size_multiplier:.2f}\n"
            )
            prev = path.read_text() if path.is_file() else "# LESSONS\n"
            if "## Risk-Guardian Audit" not in prev:
                prev = prev.rstrip() + "\n\n## Risk-Guardian Audit\n"
            write_text(path, prev.rstrip() + "\n" + block)
        except Exception as exc:  # noqa: BLE001
            logger.debug("rgmc lessons append: %s", exc)

    def _update_state_md(self) -> None:
        try:
            update_state_field("Autonomy WR", f"{self.rolling_wr():.1%}")
            update_state_field("Autonomy DD", f"{self.drawdown():.1%}")
            update_state_field(
                "Autonomy Soft κ",
                f"{self.state.soft_kappa_scale:.2f}",
            )
            update_state_field(
                "Autonomy Size×",
                f"{self.state.size_multiplier:.2f}",
            )
            update_state_field(
                "Autonomy Resolved",
                str(self.state.n_resolved),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("rgmc state md: %s", exc)

    def effective_kappa(self, kappa_base: float) -> float:
        return soft_kappa(kappa_base, self.state.soft_kappa_scale)

    def needs_rollback(self) -> bool:
        return (
            self.state.rolling_n >= 25
            and self.rolling_wr() < ROLLBACK_WR_FLOOR
            and self.state.prod_model_version is not None
        )

    def self_report(self) -> dict[str, Any]:
        return {
            "n_resolved": self.state.n_resolved,
            "rolling_wr": self.rolling_wr(),
            "drawdown": self.drawdown(),
            "soft_kappa_scale": self.state.soft_kappa_scale,
            "size_multiplier": self.state.size_multiplier,
            "disabled_families": list(self.state.disabled_families),
            "target_met": (
                self.state.n_resolved >= 200 and self.rolling_wr() >= 0.80
            ),
        }
