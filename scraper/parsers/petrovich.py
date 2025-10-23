"""Parser implementation for moscow.petrovich.ru."""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Iterable, Iterator, List, Optional

from bs4 import BeautifulSoup

from .base import BaseParser, PriceNotFoundError, ProductSnapshot, to_decimal

LOGGER = logging.getLogger(__name__)

_SCRIPT_PRICE_PATTERN = re.compile(
    r"\"(?:price|currentPrice|current)\"\s*[:=]\s*\"?(?P<price>\d+(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)


class PetrovichParser(BaseParser):
    """Parser for Petrovich store."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")

        jsonld_product = self._extract_jsonld_product(soup, url)

        title: Optional[str] = None
        sku: Optional[str] = None
        if jsonld_product:
            title = jsonld_product.get("name") or jsonld_product.get("title") or title
            sku = jsonld_product.get("sku") or jsonld_product.get("productID") or sku

        price = self._extract_price(soup, url, jsonld_product=jsonld_product)

        if not title:
            header = soup.select_one("h1")
            title = header.get_text(strip=True) if header else None

        return ProductSnapshot(url=url, price=price, currency="RUB", title=title, sku=sku, variant_key=variant)

    def parse_price(self, html: str, url: str | None = None) -> Decimal:
        soup = BeautifulSoup(html, "lxml")
        return self._extract_price(soup, url)

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
    def _extract_price(
        self,
        soup: BeautifulSoup,
        url: str | None,
        *,
        jsonld_product: Optional[dict] = None,
    ) -> Decimal:
        product_data = jsonld_product or self._extract_jsonld_product(soup, url)
        if product_data:
            price = self._price_from_jsonld(product_data, url)
            if price is not None:
                LOGGER.info("Petrovich: price via JSON-LD = %s", price)
                return price

        element = soup.select_one("[data-test='product-retail-price']")
        if element:
            text = element.get_text(strip=True)
            if text:
                try:
                    price = to_decimal(text)
                except PriceNotFoundError:
                    LOGGER.debug("Petrovich data-test price invalid", extra={"url": url})
                else:
                    LOGGER.info("Petrovich: price via [data-test] = %s", price)
                    return price

        meta = soup.select_one("meta[itemprop='price']")
        if meta:
            content = meta.get("content")
            if content:
                try:
                    price = to_decimal(content)
                except PriceNotFoundError:
                    LOGGER.debug("Petrovich meta price invalid", extra={"url": url})
                else:
                    LOGGER.info("Petrovich: price via meta[itemprop='price'] = %s", price)
                    return price

        next_data_price = self._price_from_next_data(soup, url)
        if next_data_price is not None:
            return next_data_price

        for selector in ("[itemprop='offers'] [itemprop='price']", "[class*='price']"):
            for node in soup.select(selector):
                text = node.get("content") or node.get_text(strip=True)
                if not text:
                    continue
                try:
                    price = to_decimal(text)
                except PriceNotFoundError:
                    continue
                LOGGER.info("Petrovich: price via fallback selector %s = %s", selector, price)
                return price

        script_price = self._price_from_scripts(soup)
        if script_price is not None:
            LOGGER.info("Petrovich: price via script regex = %s", script_price)
            return script_price

        LOGGER.warning("Petrovich price not found", extra={"url": url})
        raise PriceNotFoundError("Price not found on Petrovich product page")

    def _extract_jsonld_product(self, soup: BeautifulSoup, url: str | None) -> Optional[dict]:
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
                if self._is_product_type(candidate.get("@type")):
                    return candidate
        return None

    def _price_from_jsonld(self, product: dict, url: str | None) -> Optional[Decimal]:
        offers = product.get("offers")
        candidates: List[object] = []
        if isinstance(offers, dict):
            candidates.append(offers.get("price"))
        elif isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict):
                    candidates.append(offer.get("price"))
        for key in ("price", "currentPrice", "priceValue"):
            if key in product:
                candidates.append(product.get(key))

        for raw in candidates:
            if raw in (None, ""):
                continue
            try:
                return to_decimal(str(raw))
            except PriceNotFoundError:
                LOGGER.debug("Petrovich JSON-LD price invalid", extra={"url": url})
                continue
        return None

    def _price_from_next_data(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        script = soup.find("script", attrs={"id": "__NEXT_DATA__", "type": "application/json"})
        if not script:
            return None
        payload = script.string or script.text or ""
        if not payload.strip():
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            LOGGER.debug("Petrovich __NEXT_DATA__ decode failed", extra={"url": url})
            return None

        product = None
        props = data.get("props")
        if isinstance(props, dict):
            page_props = props.get("pageProps")
            if isinstance(page_props, dict):
                product = page_props.get("product")

        if isinstance(product, dict):
            price_section = product.get("price")
            if isinstance(price_section, dict):
                for key in ("current", "value", "currentPrice", "price"):
                    raw = price_section.get(key)
                    if raw in (None, ""):
                        continue
                    try:
                        price = to_decimal(str(raw))
                    except PriceNotFoundError:
                        continue
                    LOGGER.info("Petrovich: price via __NEXT_DATA__.price.%s = %s", key, price)
                    return price
            for key in ("price", "currentPrice", "current"):
                raw = product.get(key)
                if raw in (None, ""):
                    continue
                try:
                    price = to_decimal(str(raw))
                except PriceNotFoundError:
                    continue
                LOGGER.info("Petrovich: price via __NEXT_DATA__.product.%s = %s", key, price)
                return price

        for candidate in self._iter_dicts(data):
            for key in ("price", "currentPrice", "current"):
                if key not in candidate:
                    continue
                raw = candidate.get(key)
                if raw in (None, ""):
                    continue
                try:
                    price = to_decimal(str(raw))
                except PriceNotFoundError:
                    continue
                LOGGER.info("Petrovich: price via __NEXT_DATA__ %s = %s", key, price)
                return price

        return None

    def _price_from_scripts(self, soup: BeautifulSoup) -> Optional[Decimal]:
        for script in soup.find_all("script"):
            text = script.string or script.text or ""
            if not text:
                continue
            match = _SCRIPT_PRICE_PATTERN.search(text)
            if not match:
                continue
            raw = match.group("price")
            try:
                return to_decimal(raw)
            except PriceNotFoundError:
                continue
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


__all__ = ["PetrovichParser"]
