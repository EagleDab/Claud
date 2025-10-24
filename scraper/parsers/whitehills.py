"""Parser implementation for whitehills.ru."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from bs4 import BeautifulSoup

from pricing.config import settings

from .base import BaseParser, PriceNotFoundError, ProductSnapshot

LOGGER = logging.getLogger(__name__)

THIN_SPACES = ("\xa0", "\u2009", "\u202F")
PRICE_JSON_KEYS = {"price", "price_value", "current", "value", "amount"}
XHR_KEYWORDS = ("price", "catalog", "product", "offer")
DOM_SELECTORS = (
    "span.price_value",
    ".values_wrapper .price_value",
    "[itemprop='offers'] [itemprop='price']",
)


def to_decimal(text: str) -> Decimal:
    t = (text or "")
    for sp in THIN_SPACES:
        t = t.replace(sp, " ")
    t = re.sub(r"[^\d.,\s]", "", t)
    t = re.sub(r"\s+", "", t).replace(",", ".")
    match = re.search(r"\d+(?:\.\d{1,2})?", t)
    if not match:
        raise ValueError(f"no numeric in: {text!r}")
    return Decimal(match.group(0))


class WhiteHillsParser(BaseParser):
    """Parser for WhiteHills store."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")

        jsonld_product = self._find_jsonld_product(soup)
        price = self._parse_price_from_soup(soup, url=url, jsonld_product=jsonld_product)
        if price is None:
            price = await self.fetch_price_via_playwright(url)
        if price is None:
            LOGGER.warning("WhiteHills price not found", extra={"url": url})
            raise PriceNotFoundError("Price not found on WhiteHills product page")

        title: Optional[str] = None
        sku: Optional[str] = None
        variant_key = variant

        if jsonld_product:
            title = jsonld_product.get("name") or jsonld_product.get("title") or title
            sku = jsonld_product.get("sku") or jsonld_product.get("mpn") or sku
            if variant_key is None:
                variant_key = jsonld_product.get("variant")

        if not title:
            header = soup.select_one("h1")
            title = header.get_text(strip=True) if header else None

        return ProductSnapshot(
            url=url,
            price=price,
            currency="RUB",
            title=title,
            sku=sku,
            variant_key=variant_key,
        )

    def parse_price(self, html: str, url: str | None = None) -> Decimal:
        soup = BeautifulSoup(html, "lxml")
        price = self._parse_price_from_soup(soup, url=url)
        if price is None:
            LOGGER.warning("WhiteHills price not found", extra={"url": url})
            raise PriceNotFoundError("Price not found on WhiteHills product page")
        return price

    async def fetch_category(self, url: str) -> list[ProductSnapshot]:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        items: list[ProductSnapshot] = []
        for card in soup.select(".collection__item, .products-list__item"):
            link = card.select_one("a")
            price_node = card.select_one(".price, .product__price")
            if not link or not price_node:
                continue
            href = link.get("href") or ""
            try:
                price_value = self.normalize_price(price_node.get_text())
            except ValueError:
                LOGGER.debug("WhiteHills category price parse failed", extra={"url": url})
                continue
            title = link.get_text(strip=True)
            items.append(
                ProductSnapshot(
                    url=href if href.startswith("http") else f"https://whitehills.ru{href}",
                    price=price_value,
                    currency="RUB",
                    title=title,
                )
            )
        return items

    async def fetch_price_via_playwright(self, url: str) -> Decimal | None:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover - optional dependency
            LOGGER.info("whitehills: Playwright unavailable: %s", exc, extra={"url": url})
            return None

        headers = self._build_headers().copy()
        headers.pop("User-Agent", None)
        price_holder: dict[str, Decimal | None] = {"value": None}

        async def extract_from_response(response) -> None:
            if price_holder["value"] is not None:
                return
            content_type = (response.headers.get("content-type") or "").lower()
            if "application/json" not in content_type:
                return
            url_lower = response.url.lower()
            if not any(keyword in url_lower for keyword in XHR_KEYWORDS):
                return
            try:
                data = await response.json()
            except Exception:
                return
            raw_value = self._extract_price_from_json(data)
            if raw_value is None:
                return
            try:
                price_holder["value"] = to_decimal(str(raw_value))
                LOGGER.info("whitehills: price via xhr = %s", price_holder["value"])
            except (InvalidOperation, ValueError):
                price_holder["value"] = None

        try:  # pragma: no cover - requires browser
            async with async_playwright() as playwright_ctx:
                launch_args = (os.environ.get("PW_LAUNCH_ARGS") or "").split()
                browser = await playwright_ctx.chromium.launch(
                    headless=settings.playwright_headless,
                    slow_mo=settings.playwright_slow_mo,
                    args=launch_args or None,
                )
                context = None
                try:
                    context = await browser.new_context(
                        user_agent=self._choose_user_agent(),
                        extra_http_headers=headers,
                    )
                    page = await context.new_page()
                    page.on(
                        "response",
                        lambda response: asyncio.create_task(extract_from_response(response)),
                    )
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                    wait_tasks = [
                        asyncio.create_task(page.wait_for_load_state("networkidle", timeout=8000)),
                        asyncio.create_task(page.wait_for_selector("span.price_value", timeout=6000)),
                    ]
                    for task in wait_tasks:
                        try:
                            await task
                        except Exception:
                            pass

                    if price_holder["value"] is None:
                        dom_text = await page.evaluate(
                            r"""
                            () => {
                              const pick = sel => { const el = document.querySelector(sel); return el && el.textContent; };
                              const candidates = [
                                "span.price_value",
                                ".values_wrapper .price_value",
                                "[itemprop='offers'] [itemprop='price']",
                                "[class*='price']"
                              ];
                              for (const s of candidates) {
                                const t = pick(s);
                                if (t && /\d/.test(t)) return t;
                              }
                              const meta = document.querySelector('meta[itemprop="price"]');
                              if (meta && meta.content) return meta.content;
                              for (const sc of document.scripts) {
                                const txt = sc.textContent || "";
                                const m = txt.match(/"(?:price|currentPrice|amount|value)"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)"?/);
                                if (m) return m[1];
                              }
                              return null;
                            }
                            """
                        )
                        if isinstance(dom_text, str):
                            try:
                                price_holder["value"] = to_decimal(dom_text)
                                LOGGER.info("whitehills: price via dom = %s", price_holder["value"])
                            except (InvalidOperation, ValueError):
                                price_holder["value"] = None
                finally:
                    if context is not None:
                        await context.close()
                    await browser.close()
        except Exception as exc:  # pragma: no cover - Playwright environment dependent
            LOGGER.info("whitehills: Playwright price fetch failed: %s", exc, extra={"url": url})
            return price_holder["value"]

        return price_holder["value"]

    # ------------------------------------------------------------------
    def _parse_price_from_soup(
        self,
        soup: BeautifulSoup,
        *,
        url: str | None = None,
        jsonld_product: Optional[dict[str, Any]] = None,
    ) -> Decimal | None:
        product = jsonld_product or self._find_jsonld_product(soup)
        if product:
            price = self._price_from_jsonld_product(product)
            if price is not None:
                LOGGER.info("whitehills: price via jsonld = %s", price)
                return price
        price = self._price_from_static_dom(soup)
        return price

    def _find_jsonld_product(self, soup: BeautifulSoup) -> Optional[dict[str, Any]]:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            text = script.string or script.text or ""
            if not text.strip():
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            for candidate in self._iter_dicts(data):
                if self._is_product_type(candidate.get("@type")):
                    return candidate
        return None

    def _price_from_jsonld_product(self, product: dict[str, Any]) -> Decimal | None:
        offers = product.get("offers")
        offer: dict[str, Any] | None
        if isinstance(offers, list):
            offer = next((item for item in offers if isinstance(item, dict)), None)
        elif isinstance(offers, dict):
            offer = offers
        else:
            offer = None
        if not offer:
            return None
        price_value = offer.get("price")
        if price_value is None:
            return None
        try:
            return to_decimal(str(price_value))
        except (InvalidOperation, ValueError):
            return None

    def _price_from_static_dom(self, soup: BeautifulSoup) -> Decimal | None:
        for selector in DOM_SELECTORS:
            element = soup.select_one(selector)
            if not element:
                continue
            text = element.get_text(" ", strip=True)
            if not text:
                continue
            try:
                price = to_decimal(text)
            except (InvalidOperation, ValueError):
                continue
            LOGGER.info("whitehills: price via dom = %s", price)
            return price
        return None

    def _iter_dicts(self, data: Any) -> Iterable[dict[str, Any]]:
        if isinstance(data, dict):
            yield data
            for value in data.values():
                yield from self._iter_dicts(value)
        elif isinstance(data, list):
            for item in data:
                yield from self._iter_dicts(item)

    def _is_product_type(self, value: Any) -> bool:
        if isinstance(value, str):
            return value.lower() == "product"
        if isinstance(value, Iterable):
            return any(isinstance(item, str) and item.lower() == "product" for item in value)
        return False

    def _extract_price_from_json(self, data: Any) -> Any:
        stack = [data]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if isinstance(key, str) and key.lower() in PRICE_JSON_KEYS:
                        if isinstance(value, (str, int, float)):
                            return value
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
        return None


__all__ = ["WhiteHillsParser"]
