"""Parser implementation for whitehills.ru."""
from __future__ import annotations

from typing import List, Optional, cast

from bs4 import BeautifulSoup

from .base import BaseParser, ProductSnapshot, ScraperError


class WhiteHillsParser(BaseParser):
    """Parser for WhiteHills store."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        json_data = self.parse_json_from_scripts(soup, ["product", "offers"])
        title = None
        sku = None
        price = None
        variant_key = variant

        if json_data:
            product = json_data.get("product") or json_data.get("item") or json_data
            title = product.get("name")
            sku = product.get("sku")
            offers = product.get("offers") or json_data.get("offers")
            if offers and isinstance(offers, list):
                offer = offers[0]
                if variant:
                    offer = next((o for o in offers if o.get("name") == variant or o.get("sku") == variant), offer)
                raw_price = cast(float | int | str | None, offer.get("price"))
                if raw_price is None:
                    raw_price = cast(float | int | str | None, offer.get("priceValue"))
                price = float(raw_price) if raw_price is not None else 0.0
                variant_key = variant or offer.get("name")
            elif "price" in product:
                product_price = cast(float | int | str | None, product.get("price"))
                if product_price is not None:
                    price = float(product_price)

        if price is None:
            price_node = soup.select_one(".product-card__price-current span, .price__current")
            if price_node:
                price = self.extract_number(price_node.get_text())

        if price is None:
            raise ScraperError("Price not found on WhiteHills product page")

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
            price = self.extract_number(price_node.get_text())
            title = link.get_text(strip=True)
            items.append(
                ProductSnapshot(
                    url=href if href.startswith("http") else f"https://whitehills.ru{href}",
                    price=price,
                    currency="RUB",
                    title=title,
                )
            )
        return items


__all__ = ["WhiteHillsParser"]
