"""Parser implementation for whitehills.ru."""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Optional

from bs4 import BeautifulSoup

from pricing.config import settings

from .base import BaseParser, PriceNotFoundError, ProductSnapshot, ScraperError

LOGGER = logging.getLogger(__name__)

_THIN_SPACES = ("\xa0", "\u2009", "\u202F")


def _normalize_price_to_decimal(text: str) -> Decimal:
    t = text or ""
    t = re.sub(r"(руб\.?|₽)", "", t, flags=re.IGNORECASE)
    for sp in _THIN_SPACES:
        t = t.replace(sp, " ")
    t = re.sub(r"\s+", "", t)
    t = t.replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", t)
    if not match:
        raise ValueError(f"no number in: {text!r}")
    return Decimal(match.group(0))


async def _price_from_dom(page) -> Decimal | None:
    try:
        await page.wait_for_selector(
            ".values_wrapper .price_value, .price_value",
            state="visible",
            timeout=10000,
        )
    except Exception:
        return None

    nodes = await page.query_selector_all(".values_wrapper .price_value, .price_value")
    texts: list[str] = []
    for element in nodes:
        try:
            if not await element.is_visible():
                continue
            text = (await element.inner_text()) or ""
            text = text.strip()
            if text:
                texts.append(text)
        except Exception:
            continue

    if not texts:
        return None

    try:
        return _normalize_price_to_decimal(texts[-1])
    except Exception:
        pass

    decimals: list[Decimal] = []
    for candidate in texts:
        try:
            decimals.append(_normalize_price_to_decimal(candidate))
        except Exception:
            continue

    return max(decimals) if decimals else None


def _price_from_jsonld_strings(json_texts: list[str]) -> Decimal | None:
    for raw in json_texts:
        data_str = (raw or "").strip()
        if not data_str:
            continue
        try:
            data = json.loads(data_str)
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue

            offers = None
            if candidate.get("@type") == "Product":
                offers = candidate.get("offers")
            elif "offers" in candidate:
                offers = candidate["offers"]

            if not offers:
                continue

            offer_items = offers if isinstance(offers, list) else [offers]
            for offer in offer_items:
                if not isinstance(offer, dict):
                    continue
                price_value = offer.get("price")
                if price_value is None:
                    continue
                try:
                    return _normalize_price_to_decimal(str(price_value))
                except Exception:
                    continue

    return None


class WhiteHillsParser(BaseParser):
    """Parser for WhiteHills store."""

    @property
    def logger(self) -> logging.Logger:
        return LOGGER

    @staticmethod
    def _to_decimal(text: str) -> Decimal:
        return _normalize_price_to_decimal(text)

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        price: Decimal | None = None

        settings_obj = getattr(self, "settings", settings)

        try:  # pragma: no cover - requires Playwright
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover - optional dependency
            self.logger.info("whitehills: playwright error: %s", exc)
        else:
            try:
                async with async_playwright() as playwright_ctx:
                    launch_args = (os.environ.get("PW_LAUNCH_ARGS") or "").split()
                    browser = await playwright_ctx.chromium.launch(
                        headless=getattr(settings_obj, "playwright_headless", True),
                        slow_mo=getattr(settings_obj, "playwright_slow_mo", 0),
                        args=launch_args or None,
                    )
                    try:
                        context = await browser.new_context(
                            locale="ru-RU",
                            timezone_id=os.environ.get("PLAYWRIGHT_TZ", "Europe/Moscow"),
                            user_agent=(
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0 Safari/537.36"
                            ),
                        )
                        try:
                            async def _route_handler(route):
                                if route.request.resource_type in {"image", "font"}:
                                    await route.abort()
                                else:
                                    await route.continue_()

                            await context.route("**/*", _route_handler)
                            page = await context.new_page()
                            await page.goto(url, wait_until="networkidle", timeout=30000)
                            await page.wait_for_timeout(500)

                            price = await _price_from_dom(page)
                            if price is not None:
                                self.logger.info("whitehills: price via playwright = %s", price)
                                return ProductSnapshot(
                                    url=url,
                                    price=price,
                                    currency="RUB",
                                    title=None,
                                    sku=None,
                                    variant_key=variant,
                                )

                            json_texts = await page.evaluate(
                                """
                                () => Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                                      .map(s => s.textContent || '')
                                """
                            )
                            price = _price_from_jsonld_strings(json_texts)
                            if price is not None:
                                self.logger.info("whitehills: price via jsonld = %s", price)
                                return ProductSnapshot(
                                    url=url,
                                    price=price,
                                    currency="RUB",
                                    title=None,
                                    sku=None,
                                    variant_key=variant,
                                )
                        finally:
                            await context.close()
                    finally:
                        await browser.close()
            except Exception as exc:  # pragma: no cover - network/browser issues
                self.logger.info("whitehills: playwright error: %s", exc)

        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        jsonld_product = self._find_jsonld_product(soup)

        price = self._parse_price_from_soup(soup, url=url, jsonld_product=jsonld_product)
        if price is None:
            self.logger.warning("whitehills: price not found", extra={"url": url})
            raise ScraperError("Price not found on WhiteHills product page")

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

        self.logger.info("whitehills: price via static = %s", price)
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
            self.logger.warning("WhiteHills price not found", extra={"url": url})
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
                self.logger.debug("WhiteHills category price parse failed", extra={"url": url})
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
                self.logger.info("whitehills: price via jsonld = %s", price, extra={"url": url})
                return price
        price = self._price_from_static_dom(soup, url=url)
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
        for key in ("price", "priceValue", "lowPrice", "highPrice", "currentPrice", "value", "amount"):
            if key in offer and offer[key] is not None:
                try:
                    return self._to_decimal(str(offer[key]))
                except Exception:
                    continue
        return None

    def _price_from_static_dom(self, soup: BeautifulSoup, url: str | None = None) -> Decimal | None:
        first_pass_selectors = ("span.price_value", ".values_wrapper .price_value")
        for selector in first_pass_selectors:
            element = soup.select_one(selector)
            if element and element.get_text(strip=True):
                text = element.get_text(" ", strip=True)
                try:
                    price = self._to_decimal(text)
                except Exception:
                    continue
                self.logger.info("whitehills: price via dom = %s", price, extra={"url": url})
                return price

        meta = soup.select_one("meta[itemprop='price']")
        if meta and meta.get("content"):
            try:
                price = self._to_decimal(meta["content"])
                self.logger.info("whitehills: price via dom = %s", price, extra={"url": url})
                return price
            except Exception:
                pass

        for selector in ("[itemprop='offers'] [itemprop='price']", "[class*='price']"):
            element = soup.select_one(selector)
            if not element:
                continue
            text = element.get_text(" ", strip=True)
            if not text:
                continue
            try:
                price = self._to_decimal(text)
            except Exception:
                continue
            self.logger.info("whitehills: price via dom = %s", price, extra={"url": url})
            return price

        for script in soup.find_all("script"):
            text = script.string or script.text or ""
            if not text:
                continue
            match = re.search(r"\"(?:price|currentPrice|amount|value|priceValue)\"\s*:\s*\"?(\d+(?:[.,]\d{1,2})?)\"?", text)
            if not match:
                continue
            try:
                price = self._to_decimal(match.group(1))
            except Exception:
                continue
            self.logger.info("whitehills: price via dom = %s", price, extra={"url": url})
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


def to_decimal(text: str) -> Decimal:
    """Backward-compatible helper for tests and other modules."""

    return WhiteHillsParser._to_decimal(text)


__all__ = ["WhiteHillsParser"]
