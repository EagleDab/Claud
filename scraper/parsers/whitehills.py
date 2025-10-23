"""Parser implementation for whitehills.ru."""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Iterable, Iterator, List, Optional

from bs4 import BeautifulSoup

from .base import BaseParser, PriceNotFoundError, ProductSnapshot

LOGGER = logging.getLogger(__name__)

PRICE_PATTERN = re.compile(r"[0-9\s]+(?:[.,][0-9]{1,2})?")


class WhiteHillsParser(BaseParser):
    """Parser for WhiteHills store."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")

        title: Optional[str] = None
        sku: Optional[str] = None
        variant_key = variant

        jsonld_product = self._extract_jsonld_product(soup, url)
        if jsonld_product:
            title = jsonld_product.get("name") or jsonld_product.get("title") or title
            sku = jsonld_product.get("sku") or jsonld_product.get("mpn") or sku
            if variant_key is None:
                variant_key = jsonld_product.get("variant")

        price, method = self._extract_price_from_soup(soup, url, jsonld_product=jsonld_product)
        if price is None:
            LOGGER.warning("WhiteHills price not found", extra={"url": url})
            raise PriceNotFoundError("Price not found on WhiteHills product page")

        LOGGER.info("WhiteHills price extracted", extra={"url": url, "method": method})

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
        """Parse a price from HTML content."""

        soup = BeautifulSoup(html, "lxml")
        price, method = self._extract_price_from_soup(soup, url)
        if price is None:
            raise PriceNotFoundError("Price not found on WhiteHills product page")
        LOGGER.info("WhiteHills price parsed", extra={"url": url, "method": method})
        return price

    async def fetch_category(self, url: str) -> List[ProductSnapshot]:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        items: List[ProductSnapshot] = []
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
        price = self._price_from_meta_tag(soup, url)
        if price is not None:
            return price, "meta"
        price = self._price_from_text_nodes(soup, url)
        if price is not None:
            return price, "text"
        return None, None

    def _extract_jsonld_product(self, soup: BeautifulSoup, url: str) -> Optional[dict]:
        scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
        if not scripts:
            LOGGER.debug("WhiteHills JSON-LD script not found", extra={"url": url})
            return None
        for script in scripts:
            text = script.string or script.text or ""
            if not text.strip():
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                LOGGER.debug("WhiteHills JSON-LD decode failed", extra={"url": url})
                continue
            for candidate in self._iter_dicts(data):
                types = candidate.get("@type")
                if self._is_product_type(types):
                    return candidate
        LOGGER.debug("WhiteHills JSON-LD product not found", extra={"url": url})
        return None

    def _price_from_jsonld(self, product: dict, url: str | None) -> Optional[Decimal]:
        offers = product.get("offers")
        if isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict):
                    value = self._first_price_value(offer)
                    if value is not None:
                        try:
                            return self.normalize_price(value)
                        except ValueError:
                            LOGGER.debug("WhiteHills JSON-LD offer price invalid", extra={"url": url})
        elif isinstance(offers, dict):
            value = self._first_price_value(offers)
            if value is not None:
                try:
                    return self.normalize_price(value)
                except ValueError:
                    LOGGER.debug("WhiteHills JSON-LD price invalid", extra={"url": url})
        else:
            LOGGER.debug("WhiteHills JSON-LD offers missing", extra={"url": url})
        return None

    def _price_from_meta_tag(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        meta = soup.select_one("meta[itemprop='price']")
        if not meta:
            LOGGER.debug("WhiteHills meta price tag not found", extra={"url": url})
            return None
        content = meta.get("content")
        if not content:
            LOGGER.debug("WhiteHills meta price empty", extra={"url": url})
            return None
        try:
            price = self.normalize_price(content)
        except ValueError:
            LOGGER.debug("WhiteHills meta price invalid", extra={"url": url})
            return None
        LOGGER.debug("WhiteHills price extracted from meta", extra={"url": url})
        return price

    def _price_from_text_nodes(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        selectors = [
            ".product__price",
            "[class*='price']",
        ]
        for selector in selectors:
            nodes = soup.select(selector)
            if not nodes:
                continue
            for node in nodes:
                text = node.get("content") or node.get_text(" ", strip=True)
                if not text:
                    continue
                match = PRICE_PATTERN.search(text)
                if not match:
                    continue
                try:
                    price = self.normalize_price(match.group(0))
                except ValueError:
                    LOGGER.debug(
                        "WhiteHills selector price invalid",
                        extra={"url": url, "selector": selector},
                    )
                    continue
                LOGGER.debug(
                    "WhiteHills price extracted from selector",
                    extra={"url": url, "selector": selector},
                )
                return price
        LOGGER.debug("WhiteHills selectors did not yield price", extra={"url": url})
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

    def _first_price_value(self, data: dict) -> Optional[object]:
        for key in ("price", "priceValue", "lowPrice", "highPrice"):
            if key in data and data[key] not in (None, ""):
                return data[key]
        return None


__all__ = ["WhiteHillsParser"]
