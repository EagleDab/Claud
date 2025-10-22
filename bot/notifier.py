"""Notification helpers for Telegram and email."""
from __future__ import annotations

import logging
from typing import Iterable, Optional

import requests

from pricing.config import settings

LOGGER = logging.getLogger(__name__)


class TelegramNotifier:
    """Simple Telegram notifier using Bot API."""

    def __init__(self, token: Optional[str] = None, recipients: Optional[Iterable[int]] = None) -> None:
        self.token = token or settings.telegram_bot_token
        self.recipients = list(recipients or settings.telegram_admin_ids)
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send_message(self, text: str, *, parse_mode: str | None = None) -> None:
        if not self.recipients:
            LOGGER.warning("No Telegram recipients configured; skipping notification")
            return
        for chat_id in self.recipients:
            try:
                response = requests.post(
                    f"{self.base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
                    timeout=10,
                )
                response.raise_for_status()
            except Exception:  # pragma: no cover - network I/O
                LOGGER.exception("Failed to send Telegram message", extra={"chat_id": chat_id})


__all__ = ["TelegramNotifier"]
