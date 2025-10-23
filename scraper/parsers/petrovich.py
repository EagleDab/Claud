"""Parser implementation for moscow.petrovich.ru."""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Iterable, Iterator, List, Optional

from bs4 import BeautifulSoup

from .base import BaseParser, PriceNotFoundError, ProductSnapshot

LOGGER = logging.getLogger(__name__)

SCRIPT_PRICE_PATTERN = re.compile(
    r'"(?:price|currentPrice|current)"\s*[:=]\s*"?(?P<price>[0-9]+(?:[.,][0-9]{1,2})?)',
    re.IGNORECASE,
)


class PetrovichParser(BaseParser):
    """Parser for Petrovich store."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")

        title: Optional[str] = None
        sku: Optional[str] = None

        jsonld_product = self._extract_jsonld_product(soup, url)
        if jsonld_product:
            title = jsonld_product.get("name") or jsonld_product.get("title") or title
            sku = jsonld_product.get("sku") or jsonld_product.get("productID") or sku
        else:
            LOGGER.debug("Petrovich JSON-LD product not found", extra={"url": url})

        price, method = self._extract_price_from_soup(soup, url, jsonld_product=jsonld_product)
        if price is None:
            LOGGER.warning("Petrovich price not found", extra={"url": url})
            raise PriceNotFoundError("Price not found on Petrovich product page")

        LOGGER.info("Petrovich price extracted", extra={"url": url, "method": method})

        if not title:
            title_node = soup.select_one("h1")
            title = title_node.get_text(strip=True) if title_node else None

        return ProductSnapshot(url=url, price=price, currency="RUB", title=title, sku=sku, variant_key=variant)

    def parse_price(self, html: str, url: str | None = None) -> Decimal:
        """Parse a price value from HTML content."""

        soup = BeautifulSoup(html, "lxml")
        price, method = self._extract_price_from_soup(soup, url)
        if price is None:
            raise PriceNotFoundError("Price not found on Petrovich product page")
        LOGGER.info("Petrovich price parsed", extra={"url": url, "method": method})
        return price

    async def fetch_category(self, url: str) -> List[ProductSnapshot]:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        items: List[ProductSnapshot] = []
        for product in soup.select("a.catalogCard"):
            href = product.get("href")
            price_node = product.select_one(".catalogCard-price")
            if not href or not price_node:
                continue
            try:
                price = self.normalize_price(price_node.get_text())
            except ValueError:
                LOGGER.debug("Petrovich category price parse failed", extra={"url": url})
                continue
            title = product.select_one(".catalogCard-title")
            items.append(
                ProductSnapshot(
                    url=href if href.startswith("http") else f"https://moscow.petrovich.ru{href}",
                    price=price,
                    currency="RUB",
                    title=title.get_text(strip=True) if title else None,
                )
            )
        return items

    # ------------------------------------------------------------------
    def _extract_price_from_soup(
        self,
        soup: BeautifulSoup,
        url: str | None,
        *,
        jsonld_product: Optional[dict] = None,
    ) -> tuple[Optional[Decimal], Optional[str]]:
        candidate = jsonld_product
        if candidate is None:
            candidate = self._extract_jsonld_product(soup, url or "")
        if candidate:
            price = self._price_from_jsonld(candidate, url)
            if price is not None:
                return price, "jsonld"
        price = self._price_from_script_blocks(soup, url)
        if price is not None:
            return price, "script"
        price = self._price_from_meta_tag(soup, url)
        if price is not None:
            return price, "meta"
        price = self._price_from_selectors(soup, url)
        if price is not None:
            return price, "selector"
        return None, None

    def _extract_jsonld_product(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
        for script in scripts:
            text = script.string or script.text or ""
            if not text.strip():
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                LOGGER.debug("Petrovich JSON-LD decode failed", extra={"url": url})
                continue
            for candidate in self._iter_dicts(data):
                types = candidate.get("@type")
                if self._is_product_type(types):
                    return candidate
        return None

    def _iter_dicts(self, data: object) -> Iterator[dict]:
        if isinstance(data, dict):
            yield data
            for value in data.values():
                yield from self._iter_dicts(value)
        elif isinstance(data, list):
            for item in data:
                yield from self._iter_dicts(item)

    def _is_product_type(self, value: object) -> bool:
        if isinstance(value, str):
            return value.lower() == "product"
        if isinstance(value, Iterable):
            return any(isinstance(item, str) and item.lower() == "product" for item in value)
        return False

    def _price_from_jsonld(self, product: dict, url: str | None) -> Optional[Decimal]:
        offers = product.get("offers")
        if isinstance(offers, dict):
            for key in ("price", "priceValue", "lowPrice", "highPrice", "currentPrice"):
                value = offers.get(key)
                if value not in (None, ""):
                    try:
                        return self.normalize_price(value)
                    except ValueError:
                        LOGGER.debug("Petrovich JSON-LD offer price invalid", extra={"url": url})
                        break
        elif isinstance(offers, list):
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                for key in ("price", "priceValue", "currentPrice"):
                    value = offer.get(key)
                    if value not in (None, ""):
                        try:
                            return self.normalize_price(value)
                        except ValueError:
                            LOGGER.debug("Petrovich JSON-LD list offer price invalid", extra={"url": url})
                            break
        if "price" in product:
            try:
                return self.normalize_price(product.get("price"))
            except ValueError:
                LOGGER.debug("Petrovich JSON-LD product price invalid", extra={"url": url})
        if "currentPrice" in product:
            try:
                return self.normalize_price(product.get("currentPrice"))
            except ValueError:
                LOGGER.debug("Petrovich JSON-LD currentPrice invalid", extra={"url": url})
        return None

    def _price_from_script_blocks(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        for script in soup.find_all("script"):
            text = script.string or script.text or ""
            if not text:
                continue
            match = SCRIPT_PRICE_PATTERN.search(text)
            if not match:
                continue
            value = match.group("price")
            try:
                price = self.normalize_price(value)
            except ValueError:
                LOGGER.debug("Petrovich script price invalid", extra={"url": url})
                continue
            LOGGER.debug("Petrovich price extracted from script", extra={"url": url})
            return price
        LOGGER.debug("Petrovich script blocks did not yield price", extra={"url": url})
        return None

    def _price_from_meta_tag(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        meta = soup.select_one("meta[itemprop='price']")
        if not meta:
            LOGGER.debug("Petrovich meta price tag not found", extra={"url": url})
            return None
        content = meta.get("content")
        if not content:
            LOGGER.debug("Petrovich meta price empty", extra={"url": url})
            return None
        try:
            price = self.normalize_price(content)
        except ValueError:
            LOGGER.debug("Petrovich meta price invalid", extra={"url": url})
            return None
        LOGGER.debug("Petrovich price extracted from meta", extra={"url": url})
        return price

    def _price_from_selectors(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        selectors = [
            "[data-qa='product-card-price']",
            "[data-test='product-card-price']",
            "[class*='price']",
        ]
        for selector in selectors:
            nodes = soup.select(selector)
            if not nodes:
                continue
            for node in nodes:
                text = node.get_text(" ", strip=True)
                if not text:
                    text = node.get("content") or ""
                if not text:
                    continue
                lowered = text.lower()
                if "₽" not in text and "руб" not in lowered and not any(ch.isdigit() for ch in text):
                    continue
                try:
                    price = self.normalize_price(text)
                except ValueError:
                    LOGGER.debug("Petrovich selector price invalid", extra={"url": url, "selector": selector})
                    continue
                LOGGER.debug("Petrovich price extracted from selector", extra={"url": url, "selector": selector})
                return price
        LOGGER.debug("Petrovich selectors did not yield price", extra={"url": url})
        return None


__all__ = ["PetrovichParser"]
