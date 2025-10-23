"""Parser implementation for whitehills.ru."""
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


class WhiteHillsParser(BaseParser):
    """Parser for WhiteHills store."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")

        jsonld_product = self._extract_jsonld_product(soup, url)

        title: Optional[str] = None
        sku: Optional[str] = None
        variant_key = variant

        if jsonld_product:
            title = jsonld_product.get("name") or jsonld_product.get("title") or title
            sku = jsonld_product.get("sku") or jsonld_product.get("mpn") or sku
            if variant_key is None:
                variant_key = jsonld_product.get("variant")

        price = self._parse_price_from_soup(soup, url, jsonld_product=jsonld_product)

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
        return self._parse_price_from_soup(soup, url)

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
                LOGGER.info("WhiteHills: price via JSON-LD = %s", jsonld_price)
                return jsonld_price
        steps_tried.append("jsonld")

        selector_price = self._price_from_primary_selectors(soup, url)
        if selector_price is not None:
            return selector_price
        steps_tried.append(".price_value")

        meta_price = self._price_from_meta_tag(soup, url)
        if meta_price is not None:
            return meta_price
        steps_tried.append("meta[itemprop='price']")

        fallback_price = self._price_from_fallback_selectors(soup, url)
        if fallback_price is not None:
            return fallback_price
        steps_tried.append("fallback-selectors")

        script_price = self._price_from_scripts(soup, url)
        if script_price is not None:
            return script_price
        steps_tried.append("script-regex")

        LOGGER.warning(
            "WhiteHills price not found",
            extra={"url": url, "steps": steps_tried},
        )
        raise PriceNotFoundError("Price not found on WhiteHills product page")

    def _extract_jsonld_product(self, soup: BeautifulSoup, url: str | None) -> Optional[dict]:
        scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
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
        offer_candidates: List[object] = []
        if isinstance(offers, dict):
            offer_candidates.append(offers.get("price"))
        elif isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict):
                    offer_candidates.append(offer.get("price"))
        for value in offer_candidates:
            if value in (None, ""):
                continue
            try:
                return self.normalize_price(value)
            except ValueError:
                LOGGER.debug("WhiteHills JSON-LD offer price invalid", extra={"url": url})
        for key in ("price", "priceValue", "currentPrice"):
            if key in product and product[key] not in (None, ""):
                try:
                    return self.normalize_price(product[key])
                except ValueError:
                    LOGGER.debug("WhiteHills JSON-LD %s invalid", key, extra={"url": url})
        return None

    def _price_from_primary_selectors(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        for selector in (".values_wrapper .price_value", "span.price_value"):
            element = soup.select_one(selector)
            if not element:
                continue
            text = element.get_text(strip=True)
            if not text:
                continue
            try:
                price = to_decimal(text).quantize(Decimal("0.01"))
            except ValueError:
                LOGGER.debug("WhiteHills selector parse failed", extra={"url": url, "selector": selector})
                continue
            LOGGER.info("WhiteHills: price via selector %s = %s", selector, price)
            return price
        return None

    def _price_from_meta_tag(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        meta = soup.select_one("meta[itemprop='price']")
        if not meta:
            return None
        content = meta.get("content")
        if not content:
            return None
        try:
            price = to_decimal(content).quantize(Decimal("0.01"))
        except ValueError:
            LOGGER.debug("WhiteHills meta price invalid", extra={"url": url})
            return None
        LOGGER.info("WhiteHills: price via meta[itemprop='price'] = %s", price)
        return price

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
                try:
                    price = to_decimal(text).quantize(Decimal("0.01"))
                except ValueError:
                    continue
                LOGGER.info("WhiteHills: price via fallback selector %s = %s", selector, price)
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
            try:
                price = to_decimal(raw).quantize(Decimal("0.01"))
            except ValueError:
                LOGGER.debug("WhiteHills script price invalid", extra={"url": url})
                continue
            LOGGER.info("WhiteHills: price via script regex = %s", price)
            return price
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


__all__ = ["WhiteHillsParser"]
