"""Celery application for background jobs."""
from __future__ import annotations

from celery import Celery

from pricing.config import settings

celery_app = Celery(
    "price_monitor",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["scheduler.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.scheduler_timezone,
    broker_connection_retry_on_startup=True,
)

__all__ = ["celery_app"]
