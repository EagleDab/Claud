"""Parser implementation for whitehills.ru."""
from __future__ import annotations

import json
import logging
from typing import Iterable, Iterator, List, Optional

from bs4 import BeautifulSoup

from .base import BaseParser, PriceNotFoundError, ProductSnapshot

LOGGER = logging.getLogger(__name__)


class WhiteHillsParser(BaseParser):
    """Parser for WhiteHills store."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")

        title: Optional[str] = None
        sku: Optional[str] = None
        variant_key = variant

        jsonld_product = self._extract_jsonld_product(soup, url)
        price = None
        if jsonld_product:
            price = self._price_from_jsonld(jsonld_product, url)
            if price is not None:
                LOGGER.debug("WhiteHills price extracted from JSON-LD", extra={"url": url})
                title = title or jsonld_product.get("name") or jsonld_product.get("title")
                sku = jsonld_product.get("sku") or jsonld_product.get("mpn")
                variant_key = variant or jsonld_product.get("variant")
            else:
                LOGGER.debug("WhiteHills JSON-LD price not found", extra={"url": url})
        else:
            LOGGER.debug("WhiteHills JSON-LD product not found", extra={"url": url})

        if price is None:
            price = self._price_from_meta(soup, url)
        if price is None:
            price = self._price_from_offers_block(soup, url)
        if price is None:
            price = self._price_from_visible_selectors(soup, url)

        if price is None:
            LOGGER.warning("WhiteHills price not found", extra={"url": url})
            raise PriceNotFoundError("Price not found on WhiteHills product page")

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

    def _price_from_jsonld(self, product: dict, url: str) -> Optional[object]:
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

    def _price_from_meta(self, soup: BeautifulSoup, url: str) -> Optional[object]:
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

    def _price_from_offers_block(self, soup: BeautifulSoup, url: str) -> Optional[object]:
        container = soup.select_one("[itemprop='offers'] [itemprop='price']")
        if not container:
            LOGGER.debug("WhiteHills offers block price tag not found", extra={"url": url})
            return None
        value = container.get("content") or container.get_text(strip=True)
        if not value:
            LOGGER.debug("WhiteHills offers block price empty", extra={"url": url})
            return None
        try:
            price = self.normalize_price(value)
        except ValueError:
            LOGGER.debug("WhiteHills offers block price invalid", extra={"url": url})
            return None
        LOGGER.debug("WhiteHills price extracted from offers block", extra={"url": url})
        return price

    def _price_from_visible_selectors(self, soup: BeautifulSoup, url: str) -> Optional[object]:
        selectors = [
            ".product-price",
            ".price",
            ".wh-price",
            "[class*='price']",
        ]
        for selector in selectors:
            nodes = soup.select(selector)
            if not nodes:
                continue
            for node in nodes:
                text = node.get_text(" ", strip=True)
                if not text:
                    continue
                lowered = text.lower()
                if "₽" not in text and "руб" not in lowered and not any(ch.isdigit() for ch in text):
                    continue
                try:
                    price = self.normalize_price(text)
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
