"""Parser implementation for whitehills.ru."""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup

from .base import BaseParser, PriceNotFoundError, ProductSnapshot

LOGGER = logging.getLogger(__name__)

PRICE_TEXT_PATTERN = re.compile(r"\d[\d\s\xa0\u2009\u202F.,]*")
NEGATIVE_CONTEXT_HINTS = (
    "мин",
    "макс",
    "от ",
    "до ",
    "опт",
    "wholesale",
    "скид",
    "discount",
    "акци",
    "sale",
)
PRICE_PATH_KEYWORDS = ("price", "prices", "cost", "amount", "sum")
FAVOURABLE_PATH_HINTS = (
    "price",
    "current",
    "value",
    "default",
    "base",
    "retail",
    "regular",
)
NEGATIVE_PATH_HINTS = (
    "old",
    "previous",
    "discount",
    "sale",
    "strike",
    "compare",
    "min",
    "max",
    "from",
    "opt",
    "bulk",
    "wholesale",
)


def _parse_decimal_value(value: str) -> Decimal:
    if value is None:
        raise PriceNotFoundError("Price text is empty")

    normalized = (
        value.replace("\xa0", " ")
        .replace("\u2009", " ")
        .replace("\u202F", " ")
    )
    normalized = normalized.replace(" ", "")
    normalized = re.sub(r"[^0-9,\.]+", "", normalized)
    normalized = normalized.replace(",", ".")

    if normalized.count(".") > 1:
        parts = normalized.split(".")
        integer_part = "".join(parts[:-1])
        fractional = parts[-1]
        normalized = f"{integer_part}.{fractional}" if fractional else integer_part

    if not normalized:
        raise PriceNotFoundError("Price text is empty")

    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise PriceNotFoundError(f"Cannot convert price value: {value!r}") from exc


def _extract_price_from_text(text: str) -> Optional[Decimal]:
    if not text:
        return None

    normalized_text = (
        text.replace("\xa0", " ")
        .replace("\u2009", " ")
        .replace("\u202F", " ")
    )

    matches: List[Tuple[int, int, Decimal]] = []
    for match in PRICE_TEXT_PATTERN.finditer(normalized_text):
        candidate = match.group()
        try:
            price = _parse_decimal_value(candidate)
        except PriceNotFoundError:
            continue

        context_start = max(0, match.start() - 40)
        context_end = min(len(normalized_text), match.end() + 40)
        context_slice = normalized_text[context_start:context_end]
        context_lower = context_slice.lower()

        priority = 1
        if any(hint in context_lower for hint in NEGATIVE_CONTEXT_HINTS):
            priority += 1

        currency_bonus = -1 if ("₽" in context_slice or "руб" in context_lower or "rub" in context_lower or "rur" in context_lower) else 0
        matches.append((priority, currency_bonus, price))

    if not matches:
        return None

    matches.sort(key=lambda item: (item[0], item[1]))
    return matches[0][2]


def _iter_price_value_paths(data: object, path: Tuple[str, ...] = ()) -> Iterator[Tuple[Tuple[str, ...], object]]:
    if isinstance(data, dict):
        for key, value in data.items():
            key_str = str(key)
            new_path = path + (key_str,)
            yield from _iter_price_value_paths(value, new_path)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_price_value_paths(item, path)
    elif isinstance(data, (int, float, Decimal)) and not isinstance(data, bool):
        yield path, data
    elif isinstance(data, str):
        yield path, data


def _contains_any(segment: str, needles: Sequence[str]) -> bool:
    lowered = segment.lower()
    return any(needle in lowered for needle in needles)


def _score_price_path(path: Sequence[str]) -> Optional[int]:
    lowered_path = [segment.lower() for segment in path if segment]
    if not lowered_path:
        return None

    if not any(_contains_any(segment, PRICE_PATH_KEYWORDS) for segment in lowered_path):
        return None

    score = 2
    if any(_contains_any(segment, FAVOURABLE_PATH_HINTS) for segment in lowered_path):
        score = 0
    elif any("current" in segment for segment in lowered_path):
        score = 1

    if any(_contains_any(segment, NEGATIVE_PATH_HINTS) for segment in lowered_path):
        score += 1

    return max(score, 0)


def _collect_price_candidates(data: object) -> List[Tuple[int, int, str, Decimal]]:
    candidates: List[Tuple[int, int, str, Decimal]] = []
    for path, raw_value in _iter_price_value_paths(data):
        if not path:
            continue
        if isinstance(raw_value, str) and not any(char.isdigit() for char in raw_value):
            continue
        try:
            price = _parse_decimal_value(str(raw_value))
        except PriceNotFoundError:
            continue

        score = _score_price_path(path)
        if score is None:
            continue

        label = ".".join(path)
        candidates.append((score, len(path), label, price))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates

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

        price = self._extract_price(soup, url, jsonld_product=jsonld_product)

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
        return self._extract_price(soup, url)

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
                return price

        element = soup.select_one("span.price_value")
        if element:
            text = element.get_text(" ", strip=True)
            price = _extract_price_from_text(text)
            if price is not None:
                LOGGER.info("WhiteHills: price via span.price_value = %s", price)
                return price
            LOGGER.debug("WhiteHills span.price_value invalid", extra={"url": url})

        meta = soup.select_one("meta[itemprop='price'][content]")
        if meta:
            content = meta.get("content")
            if content:
                price = _extract_price_from_text(content)
                if price is not None:
                    LOGGER.info("WhiteHills: price via meta[itemprop='price'] = %s", price)
                    return price
                LOGGER.debug("WhiteHills meta price invalid", extra={"url": url})

        attribute_price = self._price_from_data_attributes(soup, url)
        if attribute_price is not None:
            return attribute_price

        script_price = self._price_from_scripts(soup)
        if script_price is not None:
            return script_price

        for selector in ("[itemprop='offers'] [itemprop='price']", "[class*='price']"):
            for node in soup.select(selector):
                text = node.get("content") or node.get_text(" ", strip=True)
                price = _extract_price_from_text(text)
                if price is None:
                    continue
                if selector == "[itemprop='offers'] [itemprop='price']":
                    LOGGER.info("WhiteHills: price via itemprop offers price = %s", price)
                else:
                    LOGGER.info("WhiteHills: price via class*='price' = %s", price)
                return price

        LOGGER.warning("WhiteHills price not found", extra={"url": url})
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
                if self._is_product_type(candidate.get("@type")):
                    return candidate
        return None

    def _price_from_jsonld(self, product: dict, url: str | None) -> Optional[Decimal]:
        sources: List[Tuple[object, str]] = []
        offers = product.get("offers")
        if isinstance(offers, (dict, list)):
            sources.append((offers, "JSON-LD offers"))
        sources.append((product, "JSON-LD"))

        for data, prefix in sources:
            candidates = _collect_price_candidates(data)
            if not candidates:
                continue
            _, _, label, price = candidates[0]
            label = label or "price"
            LOGGER.info("WhiteHills: price via %s.%s = %s", prefix, label, price)
            return price
        LOGGER.debug("WhiteHills JSON-LD price not found", extra={"url": url})
        return None

    def _price_from_scripts(self, soup: BeautifulSoup) -> Optional[Decimal]:
        for script in soup.find_all("script"):
            text = script.string or script.text or ""
            if not text.strip():
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue

            candidates = _collect_price_candidates(data)
            if candidates:
                _, _, label, price = candidates[0]
                LOGGER.info("WhiteHills: price via inline script %s = %s", label or "price", price)
                return price
        return None

    def _price_from_data_attributes(self, soup: BeautifulSoup, url: str | None) -> Optional[Decimal]:
        best: Optional[Tuple[int, Decimal, str]] = None
        for element in soup.find_all(True):
            attrs = getattr(element, "attrs", {})
            for attr, raw_value in attrs.items():
                if not isinstance(attr, str):
                    continue
                attr_lower = attr.lower()
                if attr_lower in {"class", "style"}:
                    continue
                if not attr_lower.startswith("data"):
                    continue
                if not any(keyword in attr_lower for keyword in ("price", "cost")):
                    continue

                values: Sequence[str]
                if isinstance(raw_value, (list, tuple)):
                    values = [str(item) for item in raw_value if item is not None]
                elif raw_value is None:
                    continue
                else:
                    values = [str(raw_value)]

                for value in values:
                    candidate = _extract_price_from_text(f"{attr_lower} {value}")
                    if candidate is None:
                        continue

                    priority = 1
                    if any(token in attr_lower for token in ("retail", "regular", "default", "base")):
                        priority = 0
                    elif any(token in attr_lower for token in ("min", "max", "old", "previous", "discount")):
                        priority = 3

                    current = (priority, candidate, attr_lower)
                    if best is None or current < best:
                        best = current

        if best is not None:
            _, price, attr_name = best
            LOGGER.info("WhiteHills: price via data attribute %s = %s", attr_name, price)
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
