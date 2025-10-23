"""Base classes for scraper adapters."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import cloudscraper
import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

from pricing.config import settings

LOGGER = logging.getLogger(__name__)

_WS_CLASS = "\u00A0\u2007\u202F\u2009" + r"\s"


def to_decimal(text: str) -> Decimal:
    """Convert an arbitrary price string to :class:`~decimal.Decimal`."""

    if text is None:
        raise ValueError("empty price text")
    cleaned = re.sub(rf"[^{_WS_CLASS}0-9.,]", "", text)
    cleaned = re.sub(rf"[{_WS_CLASS}]+", "", cleaned)
    cleaned = cleaned.replace(",", ".")
    match = re.search(r"^\d+(?:\.\d{1,2})?$", cleaned)
    if not match:
        match = re.search(r"\d+(?:\.\d{1,2})?", cleaned)
    if not match:
        raise ValueError(f"cannot parse decimal from: {text!r}")
    return Decimal(match.group(0))


class ScraperError(RuntimeError):
    """Raised when scraping fails."""


class PriceNotFoundError(ScraperError):
    """Raised when a price cannot be extracted from a page."""


@dataclass
class ProductSnapshot:
    """Normalized representation of a product returned by an adapter."""

    url: str
    price: Decimal | float
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
        self._cloudscraper_fallbacks = 0
        self._consecutive_antibot = 0
        self._antibot_dumped = False

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
        headers = self._build_headers()
        for attempt in range(1, settings.http_retries + 1):
            try:
                response = self._session.get(url, headers=headers, timeout=settings.http_timeout)
                if self._is_antibot_response(response):
                    LOGGER.warning(
                        "Anti-bot detected during requests fetch", extra={"url": url, "status": response.status_code}
                    )
                    self._record_antibot(url, response.text)
                    time.sleep(settings.anti_bot_delay_seconds)
                    headers = self._build_headers()
                    continue
                response.raise_for_status()
                self._reset_antibot()
                return response.text
            except Exception as exc:  # pragma: no cover - network dependent
                LOGGER.warning("Primary fetch failed", exc_info=exc)
                time.sleep(settings.anti_bot_delay_seconds)
                headers = self._build_headers()

        LOGGER.info("Falling back to cloudscraper", extra={"url": url})
        self._cloudscraper_fallbacks += 1
        if self._cloudscraper_fallbacks > 1:
            LOGGER.warning(
                "Cloudscraper fallback triggered again", extra={"url": url, "count": self._cloudscraper_fallbacks}
            )
        try:
            result = self._scraper.get(url, headers=headers, timeout=settings.http_timeout)
            result.raise_for_status()
            if self._is_antibot_response(result):
                self._record_antibot(url, result.text)
            else:
                self._reset_antibot()
                return result.text
        except Exception as exc:  # pragma: no cover - network dependent
            LOGGER.error("Cloudscraper failed", exc_info=exc)

        LOGGER.info("Falling back to Playwright", extra={"url": url})
        return asyncio.run(self._fetch_with_playwright(url))

    def _build_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._choose_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

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

    def _record_antibot(self, url: str, html: str | None) -> None:
        self._consecutive_antibot += 1
        if html and self._consecutive_antibot >= 3 and not self._antibot_dumped:
            try:
                self._dump_debug_html(url, html)
            except Exception:  # pragma: no cover - filesystem issues
                LOGGER.debug("Failed to dump anti-bot HTML", exc_info=True, extra={"url": url})
            else:
                self._antibot_dumped = True

    def _reset_antibot(self) -> None:
        self._consecutive_antibot = 0
        self._antibot_dumped = False

    def _dump_debug_html(self, url: str, html: str) -> None:
        host = urlparse(url).hostname or "unknown"
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        dump_dir = Path(".debug_dumps")
        dump_dir.mkdir(parents=True, exist_ok=True)
        path = dump_dir / f"{host}_{timestamp}.html"
        snippet = html[:3000]
        path.write_text(snippet, encoding="utf-8")
        LOGGER.debug("Saved anti-bot debug dump", extra={"url": url, "path": str(path)})

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
            self._reset_antibot()
            return content

    # ------------------------------------------------------------------
    def parse_json_from_scripts(self, soup: BeautifulSoup, keys: Iterable[str]) -> Dict[str, Any]:
        """Extract JSON data from script tags containing specified keys."""

        for script in soup.find_all("script"):
            text = script.string or script.text
            if not text:
                continue
            if not any(key in text for key in keys):
                continue
            for candidate in self._extract_json_candidates(text):
                for data in self._try_load_json(candidate):
                    if any(self._json_contains_key(data, key) for key in keys):
                        return data
        return {}

    def _extract_json_candidates(self, text: str) -> List[str]:
        """Return possible JSON snippets embedded in arbitrary script text."""

        results: List[str] = []
        stack: List[str] = []
        start_index: Optional[int] = None
        in_string = False
        escape = False
        string_char = ""

        for index, char in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == string_char:
                    in_string = False
                continue

            if char in ('"', "'"):
                in_string = True
                string_char = char
                continue

            if char in "{[":
                if not stack:
                    start_index = index
                stack.append("}" if char == "{" else "]")
                continue

            if char in "}]":
                if stack and char == stack[-1]:
                    stack.pop()
                    if not stack and start_index is not None:
                        results.append(text[start_index : index + 1])
                        start_index = None
                else:
                    stack.clear()
                    start_index = None
                continue

        return results

    def _try_load_json(self, candidate: str) -> List[Dict[str, Any]]:
        """Attempt to load JSON ensuring dictionary candidates."""

        candidate = candidate.strip()
        if candidate.endswith(";"):
            candidate = candidate[:-1]

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return []

        if isinstance(parsed, dict):
            return [parsed]

        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]

        return []

    def _json_contains_key(self, data: Dict[str, Any], target: str) -> bool:
        """Check recursively whether a key is present in a JSON-like structure."""

        def _walk(value: Any) -> bool:
            if isinstance(value, dict):
                if target in value:
                    return True
                return any(_walk(v) for v in value.values())
            if isinstance(value, list):
                return any(_walk(item) for item in value)
            return False

        return _walk(data)

    def extract_number(self, text: str) -> float:
        text = text.replace("\xa0", " ")
        cleaned = re.sub(r"[^0-9,\.]+", "", text).replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            raise ScraperError(f"Cannot parse price from '{text}'")

    def normalize_price(self, value: Any) -> Decimal:
        """Normalize incoming price values to ``Decimal`` with two decimals."""

        if value is None:
            raise ValueError("Price value is None")
        if isinstance(value, Decimal):
            decimal_value = value
        elif isinstance(value, (int, float)):
            decimal_value = Decimal(str(value))
        elif isinstance(value, str):
            try:
                decimal_value = to_decimal(value)
            except ValueError as exc:
                raise ValueError(f"No numeric value in '{value}'") from exc
        else:
            raise TypeError(f"Unsupported price type: {type(value)!r}")

        try:
            return decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except InvalidOperation as exc:
            raise ValueError(f"Cannot quantize decimal value '{decimal_value}'") from exc

    def build_variant_key(self, parts: Iterable[str]) -> str:
        items = [part.strip() for part in parts if part]
        return "|".join(items)


__all__ = [
    "BaseParser",
    "PriceNotFoundError",
    "ProductSnapshot",
    "ScraperError",
    "to_decimal",
]
