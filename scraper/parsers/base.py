"""Base classes for scraper adapters."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import cloudscraper
import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

from pricing.config import settings

LOGGER = logging.getLogger(__name__)


class ScraperError(RuntimeError):
    """Raised when scraping fails."""


@dataclass(slots=True)
class ProductSnapshot:
    """Normalized representation of a product returned by an adapter."""

    url: str
    price: float
    currency: str
    title: Optional[str] = None
    sku: Optional[str] = None
    variant_key: Optional[str] = None
    payload: Dict[str, Any] | None = None

    def __post_init__(self) -> None:  # pragma: no cover - simple validation
        if self.price < 0:
            raise ValueError("Price must be non-negative")


class BaseParser:
    """Base class for all site-specific parsers."""

    anti_bot_patterns = ("captcha", "cloudflare", "access denied")

    def __init__(self) -> None:
        self._session = requests.Session()
        self._scraper = cloudscraper.create_scraper()
        self._user_agent_provider = UserAgent()

    # ------------------------------------------------------------------
    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        """Fetch a single product."""

        raise NotImplementedError

    async def fetch_category(self, url: str) -> List[ProductSnapshot]:
        """Fetch multiple products from a category page."""

        raise NotImplementedError

    # ------------------------------------------------------------------
    async def fetch_html(self, url: str) -> str:
        """Fetch HTML with retries and anti-bot mitigation."""

        return await asyncio.to_thread(self._fetch_html_sync, url)

    def _fetch_html_sync(self, url: str) -> str:
        headers = {"User-Agent": self._choose_user_agent(), "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
        for attempt in range(1, settings.http_retries + 1):
            try:
                response = self._session.get(url, headers=headers, timeout=settings.http_timeout)
                if self._is_antibot_response(response):
                    LOGGER.warning(
                        "Anti-bot detected during requests fetch", extra={"url": url, "status": response.status_code}
                    )
                    time.sleep(settings.anti_bot_delay_seconds)
                    continue
                response.raise_for_status()
                return response.text
            except Exception as exc:  # pragma: no cover - network dependent
                LOGGER.warning("Primary fetch failed", exc_info=exc)
                time.sleep(settings.anti_bot_delay_seconds)

        LOGGER.info("Falling back to cloudscraper", extra={"url": url})
        try:
            result = self._scraper.get(url, headers=headers, timeout=settings.http_timeout)
            result.raise_for_status()
            return result.text
        except Exception as exc:  # pragma: no cover - network dependent
            LOGGER.error("Cloudscraper failed", exc_info=exc)

        LOGGER.info("Falling back to Playwright", extra={"url": url})
        return asyncio.run(self._fetch_with_playwright(url))

    def _is_antibot_response(self, response: requests.Response) -> bool:
        if response.status_code in (403, 429):
            return True
        text = response.text.lower()
        return any(pattern in text for pattern in self.anti_bot_patterns)

    def _choose_user_agent(self) -> str:
        try:  # pragma: no cover - dynamic library
            return self._user_agent_provider.random
        except Exception:
            return settings.user_agent

    # ------------------------------------------------------------------
    async def _fetch_with_playwright(self, url: str) -> str:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:  # pragma: no cover - requires browser
            browser = await p.chromium.launch(headless=settings.playwright_headless, slow_mo=settings.playwright_slow_mo)
            context = await browser.new_context(user_agent=self._choose_user_agent())
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle")
            await asyncio.sleep(1)
            content = await page.content()
            await browser.close()
            return content

    # ------------------------------------------------------------------
    def parse_json_from_scripts(self, soup: BeautifulSoup, keys: Iterable[str]) -> Dict[str, Any]:
        """Extract JSON data from script tags containing specified keys."""

        pattern = re.compile(r"({.+})", re.S)
        for script in soup.find_all("script"):
            text = script.string or script.text
            if not text:
                continue
            if not any(key in text for key in keys):
                continue
            match = pattern.search(text)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue
        return {}

    def extract_number(self, text: str) -> float:
        text = text.replace("\xa0", " ")
        cleaned = re.sub(r"[^0-9,\.]+", "", text).replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            raise ScraperError(f"Cannot parse price from '{text}'")

    def build_variant_key(self, parts: Iterable[str]) -> str:
        items = [part.strip() for part in parts if part]
        return "|".join(items)


__all__ = ["BaseParser", "ProductSnapshot", "ScraperError"]
