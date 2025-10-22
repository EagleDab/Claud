"""Parser implementation for mk4s.ru with support for product variants."""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from .base import BaseParser, ProductSnapshot, ScraperError


class MK4SParser(BaseParser):
    """Parser for MK4S which exposes variants via embedded JSON state."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        data = self.parse_json_from_scripts(soup, ["variants", "product", "sku"])
        if not data:
            raise ScraperError("MK4S product JSON not found")

        product = data.get("product") or data
        title = product.get("title") or product.get("name")
        sku = product.get("sku")
        variants: Dict[str, Dict] = {}
        for key in ("variants", "offers", "items"):
            variants_data = product.get(key) or data.get(key)
            if isinstance(variants_data, dict):
                variants.update(variants_data)
            elif isinstance(variants_data, list):
                for item in variants_data:
                    name = item.get("name") or item.get("sku") or item.get("id")
                    if name:
                        variants[name] = item
        chosen = None
        if variant and variant in variants:
            chosen = variants[variant]
        elif variants:
            chosen = next(iter(variants.values()))
            if not variant:
                variant = chosen.get("name") or chosen.get("sku") or chosen.get("id")
        price = None
        if chosen:
            sku = chosen.get("sku") or sku
            price = chosen.get("price") or chosen.get("priceValue") or chosen.get("value")
        if price is None and "price" in product:
            price = product.get("price")
        if price is None:
            price_node = soup.select_one("[data-product-price], .price--current, .product-price")
            if price_node:
                price = self.extract_number(price_node.get_text())
        if price is None:
            raise ScraperError("Price not found on MK4S product page")

        variant_key = variant
        if chosen and not variant_key:
            variant_key = self.build_variant_key([chosen.get("name"), chosen.get("id")])

        return ProductSnapshot(
            url=url,
            price=float(price),
            currency="RUB",
            title=title,
            sku=sku,
            variant_key=variant_key,
            payload={"variant": chosen} if chosen else None,
        )

    async def fetch_category(self, url: str) -> List[ProductSnapshot]:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        items: List[ProductSnapshot] = []
        for container in soup.select("[data-product]"):
            data_attr = container.get("data-product")
            if not data_attr:
                continue
            price = None
            if data_attr.startswith("{"):
                try:
                    product_json = json.loads(data_attr)
                except Exception:
                    product_json = {}
                price = product_json.get("price") or product_json.get("priceValue")
            if price is None:
                price_node = container.select_one(".price, .product-card__price")
                if price_node:
                    price = self.extract_number(price_node.get_text())
            link = container.select_one("a")
            href = link.get("href") if link else None
            if not price or not href:
                continue
            title = link.get_text(strip=True) if link else None
            items.append(
                ProductSnapshot(
                    url=href if href.startswith("http") else f"https://mk4s.ru{href}",
                    price=float(price),
                    currency="RUB",
                    title=title,
                )
            )
        return items


__all__ = ["MK4SParser"]
