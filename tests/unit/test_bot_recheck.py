"""Tests for manual recheck error handling in the Telegram bot."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from bot import main as bot_main
from msklad import MoySkladError
from scraper import ScraperError


class DummyQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def edit_message_text(self, text: str, *args, **kwargs):  # pragma: no cover - interface compliance
        self.messages.append(text)


class DummySession:
    def __init__(self, product: object) -> None:
        self._product = product
        self.flushed = False

    def get(self, model, product_id):  # pragma: no cover - signature compatibility
        return self._product

    def flush(self) -> None:
        self.flushed = True


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: DummySession) -> None:
    @contextmanager
    def fake_scope():
        yield session

    monkeypatch.setattr(bot_main, "session_scope", fake_scope)


@pytest.mark.asyncio
async def test_perform_recheck_reports_scraper_error(monkeypatch: pytest.MonkeyPatch) -> None:
    query = DummyQuery()
    session = DummySession(SimpleNamespace())
    _patch_session(monkeypatch, session)

    class FailingService:
        def __init__(self, _session):
            assert _session is session

        async def check_product(self, product):
            raise ScraperError("network timeout")

    monkeypatch.setattr(bot_main, "PriceMonitorService", FailingService)

    await bot_main.perform_recheck(query, 1)

    assert query.messages == ["Не удалось проверить товар: network timeout"]
    assert session.flushed is False


@pytest.mark.asyncio
async def test_perform_recheck_reports_msklad_error(monkeypatch: pytest.MonkeyPatch) -> None:
    query = DummyQuery()
    session = DummySession(SimpleNamespace())
    _patch_session(monkeypatch, session)

    class FailingService:
        def __init__(self, _session):
            assert _session is session

        async def check_product(self, product):
            raise MoySkladError("api unavailable")

    monkeypatch.setattr(bot_main, "PriceMonitorService", FailingService)

    await bot_main.perform_recheck(query, 5)

    assert query.messages == ["Не удалось обновить цену в МойСклад: api unavailable"]
    assert session.flushed is False

