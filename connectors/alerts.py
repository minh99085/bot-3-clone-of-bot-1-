"""Slack / Telegram alert connector."""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class AlertClient:
    def __init__(
        self,
        slack_webhook: Optional[str] = None,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
    ):
        self.slack_webhook = slack_webhook or os.environ.get("SLACK_WEBHOOK_URL")
        self.telegram_token = telegram_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    def send(self, message: str, *, level: str = "info") -> None:
        prefix = {"info": "", "warn": "WARNING: ", "error": "ALERT: "}.get(level, "")
        text = f"[Hermes] {prefix}{message}"
        sent = False
        if self.slack_webhook:
            try:
                httpx.post(self.slack_webhook, json={"text": text}, timeout=10.0)
                sent = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("slack alert failed: %s", exc)
        if self.telegram_token and self.telegram_chat_id:
            try:
                url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
                httpx.post(
                    url,
                    json={"chat_id": self.telegram_chat_id, "text": text},
                    timeout=10.0,
                )
                sent = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram alert failed: %s", exc)
        if not sent:
            logger.info("alert (no channel): %s", text)
