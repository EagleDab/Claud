"""Parser implementation for moscow.petrovich.ru."""
from __future__ import annotations

from typing import List, Optional

from bs4 import BeautifulSoup

from .base import BaseParser, ProductSnapshot, ScraperError


class PetrovichParser(BaseParser):
    """Parser for Petrovich store."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        json_data = self.parse_json_from_scripts(soup, ["product", "sku"])
        title = None
        sku = None
        price = None
        if json_data:
            product = json_data.get("product") or json_data.get("productCard") or json_data
            title = product.get("title") or product.get("name")
            sku = product.get("sku") or product.get("code")
            price = float(product.get("price") or product.get("currentPrice") or 0)

        if price is None or price == 0:
            price_node = soup.select_one("[data-test='product-card-price'] span, .product-price__current")
            if price_node:
                price = self.extract_number(price_node.get_text())

        if price is None:
            raise ScraperError("Price not found on Petrovich product page")

        if not title:
            title_node = soup.select_one("h1")
            title = title_node.get_text(strip=True) if title_node else None

        return ProductSnapshot(url=url, price=price, currency="RUB", title=title, sku=sku, variant_key=variant)

    async def fetch_category(self, url: str) -> List[ProductSnapshot]:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        items: List[ProductSnapshot] = []
        for product in soup.select("a.catalogCard"):
            href = product.get("href")
            price_node = product.select_one(".catalogCard-price")
            if not href or not price_node:
                continue
            price = self.extract_number(price_node.get_text())
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


__all__ = ["PetrovichParser"]
