"""Scheduler entry point using APScheduler to trigger Celery tasks."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from pricing.config import settings
from scheduler.celery_app import celery_app  # noqa: F401
from scheduler.tasks import check_prices_task

LOGGER = logging.getLogger(__name__)


async def trigger_job() -> None:
    LOGGER.info("Enqueueing price check task")
    check_prices_task.delay()  # type: ignore[attr-defined]


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
    scheduler.add_job(
        lambda: asyncio.create_task(trigger_job()),
        IntervalTrigger(minutes=settings.default_poll_interval_minutes),
        id="price-check",
        replace_existing=True,
    )
    scheduler.start()
    LOGGER.info("Scheduler started with interval %s min", settings.default_poll_interval_minutes)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
