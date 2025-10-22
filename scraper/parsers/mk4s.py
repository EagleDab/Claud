"""Parser implementation for mk4s.ru with support for product variants."""
from __future__ import annotations

import json
import re
from itertools import product as iter_product
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from .base import BaseParser, ProductSnapshot, ScraperError


class MK4SParser(BaseParser):
    """Parser for MK4S which exposes variants via embedded JSON state."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        data = self.parse_json_from_scripts(soup, ["variants", "product", "sku"])

        snapshot = None
        if data:
            snapshot = self._build_snapshot_from_json(url, soup, data, variant)
        if snapshot is None:
            snapshot = self._build_snapshot_from_dom(url, soup, variant)
        if snapshot is None:
            raise ScraperError("MK4S product data not found")
        return snapshot

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

    # ------------------------------------------------------------------
    def _build_snapshot_from_json(
        self, url: str, soup: BeautifulSoup, data: Dict[str, Any], variant: Optional[str]
    ) -> Optional[ProductSnapshot]:
        product = self._find_product_dict(data)
        if not product:
            return None

        title = product.get("title") or product.get("name")
        sku = product.get("sku") or product.get("id")

        variants = self._collect_variants_from_json(product, data)
        chosen = None
        variant_key = variant
        matched = False
        if variant and variant in variants:
            chosen = variants[variant]
            matched = True
        elif variants:
            chosen_key, chosen = next(iter(variants.items()))
            if not variant_key:
                variant_key = chosen.get("name") or chosen.get("sku") or chosen.get("id") or chosen_key

        price = None
        if chosen:
            sku = chosen.get("sku") or sku
            price = self._extract_price_value(
                chosen.get("price") or chosen.get("priceValue") or chosen.get("value")
            )

        if price is None:
            price = self._extract_price_value(product.get("price") or data.get("price"))

        if price is None:
            price = self._find_price_in_dom(soup)

        if price is None:
            return None

        if not matched and variant and chosen:
            variant_key = variant_key or variant

        payload: Dict[str, Any] | None = {"variant": chosen} if chosen else None

        return ProductSnapshot(
            url=url,
            price=price,
            currency="RUB",
            title=title,
            sku=sku,
            variant_key=variant_key,
            payload=payload,
        )

    def _build_snapshot_from_dom(
        self, url: str, soup: BeautifulSoup, variant: Optional[str]
    ) -> Optional[ProductSnapshot]:
        price = self._find_price_in_dom(soup)
        if price is None:
            return None

        title_node = soup.select_one(".product__title, h1")
        title = title_node.get_text(strip=True) if title_node else None

        blocks = self._extract_variant_blocks(soup)
        combos = self._build_variant_combinations(blocks)

        chosen_variant_map: Optional[Dict[str, str]] = None
        variant_key = variant
        if combos:
            chosen_variant_map, computed_key, matched = self._select_dom_variant(combos, variant)
            variant_key = variant if matched else computed_key

        payload = None
        if chosen_variant_map:
            payload = {"variant": chosen_variant_map}

        return ProductSnapshot(
            url=url,
            price=price,
            currency="RUB",
            title=title,
            variant_key=variant_key,
            payload=payload,
        )

    def _find_product_dict(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(data, dict):
            return None

        product = data.get("product")
        if isinstance(product, dict):
            return product
        if isinstance(product, list):
            for item in product:
                if isinstance(item, dict):
                    return item

        for key in ("data", "state", "props", "items", "pageData"):
            nested = data.get(key)
            if isinstance(nested, dict):
                found = self._find_product_dict(nested)
                if found:
                    return found
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        found = self._find_product_dict(item)
                        if found:
                            return found

        if any(k in data for k in ("variants", "offers", "items", "sku", "price")):
            return data

        return None

    def _collect_variants_from_json(self, product: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        variants: Dict[str, Dict[str, Any]] = {}
        for key in ("variants", "offers", "items"):
            variants_data = product.get(key) or data.get(key)
            if isinstance(variants_data, dict):
                for name, value in variants_data.items():
                    if isinstance(value, dict):
                        variants[name] = value
            elif isinstance(variants_data, list):
                for item in variants_data:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name") or item.get("sku") or item.get("id")
                    if name:
                        variants[name] = item
        return variants

    def _extract_price_value(self, price: Any) -> Optional[float]:
        if price is None:
            return None
        if isinstance(price, (int, float)):
            return float(price)
        if isinstance(price, str):
            try:
                return float(price)
            except ValueError:
                try:
                    return self.extract_number(price)
                except ScraperError:
                    return None
        return None

    def _find_price_in_dom(self, soup: BeautifulSoup) -> Optional[float]:
        price_selectors = [
            ".product-add-to-cart__price",
            "[data-product-price]",
            ".price--current",
            ".product-price__current",
            ".product-price",
            ".product__price",
        ]
        for selector in price_selectors:
            node = soup.select_one(selector)
            if not node:
                continue
            text = node.get_text(" ", strip=True)
            if not text:
                continue
            try:
                return self.extract_number(text)
            except ScraperError:
                continue
        return None

    def _extract_variant_blocks(self, soup: BeautifulSoup) -> List[Tuple[str, List[str]]]:
        blocks: List[Tuple[str, List[str]]] = []
        for block in soup.select(".block.block_secondary"):
            header = block.select_one(".block__header, .block__title")
            if not header:
                continue
            name = header.get_text(strip=True).rstrip(":")
            if not name:
                continue
            variants: List[str] = []

            for selector in [
                ".product-feature-select__color-wrapper .tooltip__content",
                ".product-feature-select__value",
                "option",
                "label",
                ".product-feature-select__item",
            ]:
                for element in block.select(selector):
                    value = self._extract_text_from_element(element)
                    if value and value not in variants:
                        variants.append(value)

            if variants:
                blocks.append((name, variants))
        return blocks

    def _extract_text_from_element(self, element: Any) -> Optional[str]:
        texts: List[str] = []
        text_content = element.get_text(" ", strip=True)
        if text_content:
            texts.append(text_content)
        for attr in ("data-value", "data-title", "title", "value", "aria-label", "data-tooltip"):
            attr_value = element.get(attr)
            if attr_value:
                texts.append(str(attr_value))
        for text in texts:
            cleaned = text.strip()
            if cleaned:
                return cleaned
        return None

    def _build_variant_combinations(self, blocks: List[Tuple[str, List[str]]]) -> List[Dict[str, str]]:
        if not blocks:
            return []
        names = [name for name, _ in blocks]
        variant_lists = [values for _, values in blocks]
        combos: List[Dict[str, str]] = []
        for combo in iter_product(*variant_lists):
            combos.append(dict(zip(names, combo)))
        return combos

    def _select_dom_variant(
        self, combos: List[Dict[str, str]], variant: Optional[str]
    ) -> Tuple[Dict[str, str], str, bool]:
        if variant:
            target_tokens = self._normalize_tokens(variant)
            for combo in combos:
                combo_tokens = self._tokens_for_combo(combo)
                combo_key = self.build_variant_key(combo.values())
                if target_tokens and target_tokens.issubset(combo_tokens):
                    return combo, combo_key, True
                if self._normalize_string(combo_key) == self._normalize_string(variant):
                    return combo, combo_key, True
        combo = combos[0]
        return combo, self.build_variant_key(combo.values()), False

    def _tokens_for_combo(self, combo: Dict[str, str]) -> set[str]:
        tokens: set[str] = set()
        for name, value in combo.items():
            tokens.update(self._normalize_tokens(name))
            tokens.update(self._normalize_tokens(value))
        return tokens

    def _normalize_tokens(self, text: str) -> set[str]:
        normalized = self._normalize_string(text)
        return {part for part in re.split(r"[|/,:;\s]+", normalized) if part}

    def _normalize_string(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().lower()) if text else ""


__all__ = ["MK4SParser"]
