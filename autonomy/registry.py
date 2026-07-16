"""MLflow-style local model registry (no external MLflow dependency).

Layout:
  data/models/registry.json
  data/models/<name>/<version>/params.json
  data/models/<name>/<version>/metrics.json

Statuses: shadow → prod → retired / rolled_back
Shadow requires SHADOW_PROMOTE_TRADES successful paper trades before promote.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from autonomy.freeze import SHADOW_PROMOTE_TRADES, assert_mutable_only
from autonomy.schemas import ModelCard, ModelStatus
from hermes.state_io import DATA, ensure_dirs

logger = logging.getLogger(__name__)


def models_root() -> Path:
    return DATA / "models"


def registry_path() -> Path:
    return models_root() / "registry.json"


class ModelRegistry:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or models_root()
        self.cards: dict[str, ModelCard] = {}
        self._load()

    def _load(self) -> None:
        path = self.root / "registry.json"
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text())
            for key, card in (raw.get("cards") or {}).items():
                self.cards[key] = ModelCard.model_validate(card)
        except Exception as exc:  # noqa: BLE001
            logger.warning("registry load failed: %s", exc)

    def save(self) -> None:
        ensure_dirs()
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "cards": {k: v.model_dump(mode="json") for k, v in self.cards.items()},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = self.root / "registry.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(path)

    def _key(self, name: str, version: str) -> str:
        return f"{name}@{version}"

    def register(
        self,
        name: str,
        params: dict[str, Any],
        metrics: Optional[dict[str, float]] = None,
        *,
        notes: str = "",
    ) -> ModelCard:
        safe = assert_mutable_only(params)
        # Microseconds avoid same-second key collisions when registering rapidly
        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        card = ModelCard(
            name=name,
            version=version,
            status=ModelStatus.SHADOW,
            metrics=dict(metrics or {}),
            params=safe,
            notes=notes,
        )
        dest = self.root / name / version
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "params.json").write_text(json.dumps(safe, indent=2))
        (dest / "metrics.json").write_text(json.dumps(metrics or {}, indent=2))
        self.cards[self._key(name, version)] = card
        self.save()
        logger.info("registry: registered %s@%s (shadow)", name, version)
        return card

    def prod_card(self, name: str = "fusion") -> Optional[ModelCard]:
        prods = [
            c
            for c in self.cards.values()
            if c.name == name and c.status == ModelStatus.PROD
        ]
        if not prods:
            return None
        return sorted(prods, key=lambda c: c.version, reverse=True)[0]

    def shadow_card(self, name: str = "fusion") -> Optional[ModelCard]:
        shadows = [
            c
            for c in self.cards.values()
            if c.name == name and c.status == ModelStatus.SHADOW
        ]
        if not shadows:
            return None
        return sorted(shadows, key=lambda c: c.version, reverse=True)[0]

    def record_shadow_trade(self, *, won: bool, name: str = "fusion") -> dict[str, Any]:
        card = self.shadow_card(name)
        if card is None:
            return {"ready": False, "reason": "no_shadow"}
        m = dict(card.metrics)
        m["shadow_n"] = float(m.get("shadow_n", 0) + 1)
        m["shadow_wins"] = float(m.get("shadow_wins", 0) + (1 if won else 0))
        card.metrics = m
        self.cards[self._key(card.name, card.version)] = card
        self.save()
        n = int(m["shadow_n"])
        wr = m["shadow_wins"] / max(1, n)
        ready = n >= SHADOW_PROMOTE_TRADES and wr >= 0.80
        return {"ready": ready, "n": n, "wr": wr, "version": card.version}

    def promote(self, name: str = "fusion") -> Optional[ModelCard]:
        shadow = self.shadow_card(name)
        if shadow is None:
            return None
        n = int(shadow.metrics.get("shadow_n", 0))
        wins = float(shadow.metrics.get("shadow_wins", 0))
        wr = wins / max(1, n)
        if n < SHADOW_PROMOTE_TRADES or wr < 0.80:
            logger.info(
                "registry: promote blocked n=%d wr=%.2f (need %d / ≥80%%)",
                n,
                wr,
                SHADOW_PROMOTE_TRADES,
            )
            return None
        # Retire old prod
        for c in list(self.cards.values()):
            if c.name == name and c.status == ModelStatus.PROD:
                c.status = ModelStatus.RETIRED
                self.cards[self._key(c.name, c.version)] = c
        shadow.status = ModelStatus.PROD
        shadow.promoted_at = datetime.now(timezone.utc)
        self.cards[self._key(shadow.name, shadow.version)] = shadow
        self.save()
        logger.info("registry: PROMOTED %s@%s wr=%.1%% n=%d", name, shadow.version, wr * 100, n)
        return shadow

    def rollback(self, name: str = "fusion") -> Optional[ModelCard]:
        prod = self.prod_card(name)
        if prod is None:
            return None
        prod.status = ModelStatus.ROLLED_BACK
        self.cards[self._key(prod.name, prod.version)] = prod
        # Restore latest retired
        retired = [
            c
            for c in self.cards.values()
            if c.name == name and c.status == ModelStatus.RETIRED
        ]
        restored = None
        if retired:
            restored = sorted(retired, key=lambda c: c.version, reverse=True)[0]
            restored.status = ModelStatus.PROD
            self.cards[self._key(restored.name, restored.version)] = restored
        self.save()
        logger.warning(
            "registry: ROLLBACK %s@%s → %s",
            name,
            prod.version,
            restored.version if restored else "none",
        )
        return restored
