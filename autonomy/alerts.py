"""Alerts for promotions / rollbacks / DD events.

Telegram + Slack webhooks via env (optional). Never spam — only
lifecycle events. Failures are logged, never raise into the loop.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _telegram_send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text[:3500]},
            )
            return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        logger.debug("telegram alert failed: %s", exc)
        return False


def _slack_send(text: str) -> bool:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json={"text": text[:3500]})
            return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        logger.debug("slack alert failed: %s", exc)
        return False


def alert(event: str, detail: str, *, meta: Optional[dict[str, Any]] = None) -> None:
    """Fire alert on promote / rollback / DD — best effort."""
    prefix = {
        "promote": "🟢 PROMOTE",
        "rollback": "🔴 ROLLBACK",
        "dd": "🟠 DD GUARD",
        "eho": "🟣 EHO",
        "info": "ℹ️",
    }.get(event, "📣")
    text = f"[Bot3 Autonomy] {prefix}: {detail}"
    if meta:
        text += "\n" + ", ".join(f"{k}={v}" for k, v in list(meta.items())[:8])
    logger.info("ALERT %s: %s", event, detail)
    _telegram_send(text)
    _slack_send(text)
