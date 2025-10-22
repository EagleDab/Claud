"""Celery tasks for monitoring and synchronization."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from celery import shared_task

from bot.notifier import TelegramNotifier
from pricing.service import check_all_products

LOGGER = logging.getLogger(__name__)


def format_event(event: Dict[str, Any]) -> str:
    product_title = event.get("product_title") or event["competitor_url"]
    parts = [
        f"💡 Обновление цены для {product_title}",
        f"Конкурент: {event['competitor_url']}",
        f"Старая цена: {event.get('old_price') or '—'}",
        f"Новая цена: {event['new_price']}",
    ]
    codes = event.get("msklad_codes") or []
    if codes:
        parts.append(f"Коды МойСклад: {', '.join(codes)}")
    price_types = event.get("price_types") or []
    if price_types:
        parts.append("Обновлены типы цен: " + ", ".join(price_types))
    return "\n".join(parts)


@shared_task(name="scheduler.check_prices")
def check_prices_task() -> int:
    """Celery task that triggers price checks and sends notifications."""

    LOGGER.info("Starting scheduled price check")
    events: List[Dict[str, Any]] = asyncio.run(check_all_products())
    if not events:
        LOGGER.info("No price changes detected")
        return 0
    notifier = TelegramNotifier()
    for event in events:
        notifier.send_message(format_event(event))
    LOGGER.info("Notifications sent", extra={"count": len(events)})
    return len(events)


__all__ = ["check_prices_task"]
