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

        price = self._parse_price_from_soup(soup, url, jsonld_product=jsonld_product)

        if not title:
            header = soup.select_one("h1")
            title = header.get_text(strip=True) if header else None

        return ProductSnapshot(url=url, price=price, currency="RUB", title=title, sku=sku, variant_key=variant)

    def parse_price(self, html: str, url: str | None = None) -> Decimal:
        soup = BeautifulSoup(html, "lxml")
        return self._parse_price_from_soup(soup, url)

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
    def _parse_price_from_soup(
        self,
        soup: BeautifulSoup,
        url: str | None,
        *,
        jsonld_product: Optional[dict] = None,
    ) -> Decimal:
        steps_tried: List[str] = []

        product_data = jsonld_product or self._extract_jsonld_product(soup, url)
        if product_data:
            jsonld_price = self._price_from_jsonld(product_data, url)
            if jsonld_price is not None:
                LOGGER.info("Petrovich: price via JSON-LD = %s", jsonld_price)
                return jsonld_price
        steps_tried.append("jsonld")

        data_test_price = self._price_from_data_test_selector(soup, url)
        if data_test_price is not None:
            return data_test_price
        steps_tried.append("[data-test='product-retail-price']")

        meta_price = self._price_from_meta_tag(soup, url)
        if meta_price is not None:
            return meta_price
        steps_tried.append("meta[itemprop='price']")

        next_data_price = self._price_from_next_data(soup, url)
        if next_data_price is not None:
            return next_data_price
        steps_tried.append("__NEXT_DATA__")

        fallback_price = self._price_from_fallback_selectors(soup, url)
        if fallback_price is not None:
            return fallback_price
        steps_tried.append("fallback-selectors")

        script_price = self._price_from_scripts(soup, url)
        if script_price is not None:
            return script_price
        steps_tried.append("script-regex")

        LOGGER.warning(
            "Petrovich price not found",
            extra={"url": url, "steps": steps_tried},
        )
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
                types = candidate.get("@type")
                if self._is_product_type(types):
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
                    candidates.append(offer.get("currentPrice"))
        for key in ("price", "currentPrice", "priceValue"):
            if key in product:
                candidates.append(product.get(key))
        for value in candidates:
            price = self._coerce_price(value, url, "jsonld")
            if price is not None:
                return price
        return None

    def _price_from_data_test_selector(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        element = soup.select_one("[data-test='product-retail-price']")
        if not element:
            return None
        text = element.get_text(strip=True)
        if not text:
            return None
        price = self._coerce_price(text, url, "[data-test='product-retail-price']")
        if price is not None:
            LOGGER.info("Petrovich: price via [data-test] = %s", price)
        return price

    def _price_from_meta_tag(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        meta = soup.select_one("meta[itemprop='price']")
        if not meta:
            return None
        content = meta.get("content")
        if not content:
            return None
        price = self._coerce_price(content, url, "meta[itemprop='price']")
        if price is not None:
            LOGGER.info("Petrovich: price via meta[itemprop='price'] = %s", price)
        return price

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
        try:
            props = data.get("props")
            if isinstance(props, dict):
                page_props = props.get("pageProps")
                if isinstance(page_props, dict):
                    product = page_props.get("product")
        except AttributeError:
            product = None

        if isinstance(product, dict):
            price_section = product.get("price")
            if isinstance(price_section, dict):
                for key in ("current", "value", "currentPrice", "price"):
                    price = self._coerce_price(price_section.get(key), url, f"__NEXT_DATA__.price.{key}")
                    if price is not None:
                        LOGGER.info("Petrovich: price via __NEXT_DATA__.price.%s = %s", key, price)
                        return price
            for key in ("price", "currentPrice", "current"):
                price = self._coerce_price(product.get(key), url, f"__NEXT_DATA__.product.{key}")
                if price is not None:
                    LOGGER.info("Petrovich: price via __NEXT_DATA__.product.%s = %s", key, price)
                    return price

        for candidate in self._iter_dicts(data):
            for key in ("price", "currentPrice", "current"):
                if key not in candidate:
                    continue
                price = self._coerce_price(candidate.get(key), url, f"__NEXT_DATA__[{key}]")
                if price is not None:
                    LOGGER.info("Petrovich: price via __NEXT_DATA__ %s = %s", key, price)
                    return price
        return None

    def _price_from_fallback_selectors(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        selectors = (
            "[itemprop='offers'] [itemprop='price']",
            "[class*='price']",
        )
        for selector in selectors:
            for node in soup.select(selector):
                text = node.get("content") or node.get_text(strip=True)
                if not text:
                    continue
                price = self._coerce_price(text, url, selector)
                if price is not None:
                    LOGGER.info("Petrovich: price via fallback selector %s = %s", selector, price)
                    return price
        return None

    def _price_from_scripts(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        for script in soup.find_all("script"):
            text = script.string or script.text or ""
            if not text:
                continue
            match = _SCRIPT_PRICE_PATTERN.search(text)
            if not match:
                continue
            raw = match.group("price")
            price = self._coerce_price(raw, url, "script")
            if price is not None:
                LOGGER.info("Petrovich: price via script regex = %s", price)
                return price
        return None

    def _coerce_price(self, value: object, url: str | None, context: str) -> Optional[Decimal]:
        if value in (None, ""):
            return None
        try:
            if isinstance(value, Decimal):
                price = value
            elif isinstance(value, (int, float)):
                price = Decimal(str(value))
            elif isinstance(value, str):
                price = to_decimal(value)
            else:
                return None
            return price.quantize(Decimal("0.01"))
        except Exception:
            LOGGER.debug("Petrovich %s price invalid", context, extra={"url": url})
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
