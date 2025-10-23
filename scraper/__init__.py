"""Scraper service orchestrating parser adapters."""
from __future__ import annotations

import asyncio
from typing import Dict, Iterable, List, Optional, Type

from structlog import get_logger

from pricing.config import settings
from scraper.parsers import (
    ADAPTER_REGISTRY,
    BaseParser,
    PriceNotFoundError,
    ProductSnapshot,
    ScraperError,
)

LOGGER = get_logger(__name__)


class ScraperService:
    """Facade around available parsers."""

    def __init__(self, registry: Optional[Dict[str, Type[BaseParser]]] = None) -> None:
        self.registry = registry or ADAPTER_REGISTRY
        self._instances: Dict[str, BaseParser] = {}
        self._lock = asyncio.Lock()

    async def _get_parser(self, adapter_name: str) -> BaseParser:
        async with self._lock:
            if adapter_name not in self._instances:
                parser_cls = self.registry.get(adapter_name)
                if not parser_cls:
                    raise ScraperError(f"Unknown parser '{adapter_name}'")
                self._instances[adapter_name] = parser_cls()
            return self._instances[adapter_name]

    async def fetch_product(self, adapter_name: str, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        parser = await self._get_parser(adapter_name)
        return await parser.fetch_product(url, variant=variant)

    async def fetch_category(self, adapter_name: str, url: str) -> List[ProductSnapshot]:
        parser = await self._get_parser(adapter_name)
        return await parser.fetch_category(url)

    async def fetch_products_parallel(
        self,
        adapter_name: str,
        urls: Iterable[str],
        *,
        concurrency: Optional[int] = None,
    ) -> List[ProductSnapshot]:
        parser = await self._get_parser(adapter_name)
        semaphore = asyncio.Semaphore(concurrency or settings.max_concurrent_requests)

        async def _fetch(u: str) -> ProductSnapshot:
            async with semaphore:
                return await parser.fetch_product(u)

        return await asyncio.gather(*[_fetch(url) for url in urls])


__all__ = ["ScraperService", "ProductSnapshot", "ScraperError", "PriceNotFoundError"]
