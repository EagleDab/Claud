"""Parser implementation for moscow.petrovich.ru."""
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
CARD_CONTEXT_HINTS = (
    "по карте",
    "карте",
    "карты",
    "карту",
    "картой",
    "card",
    "bonus",
    "loyal",
    "club",
)
REGULAR_CONTEXT_HINTS = (
    "без карты",
    "не по карте",
    "обыч",
    "рознич",
    "retail",
    "regular",
    "стандарт",
    "standard",
    "default",
)
NEGATIVE_CONTEXT_HINTS = (
    "мин",
    "макс",
    "от ",
    "до ",
    "sale",
    "скид",
    "discount",
    "акци",
)
PRICE_PATH_KEYWORDS = ("price", "prices", "cost", "amount", "sum")
REGULAR_PATH_HINTS = (
    "retail",
    "regular",
    "withoutcard",
    "no_card",
    "nocard",
    "cardless",
    "default",
    "base",
    "standard",
    "normal",
    "usual",
)
CARD_PATH_HINTS = ("card", "bonus", "loyal", "club")
CARD_EXCLUSION_HINTS = ("withoutcard", "no_card", "nocard", "cardless")
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
    "credit",
    "installment",
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


def _extract_price_from_text(text: str, *, prefer_regular: bool = False) -> Optional[Decimal]:
    if not text:
        return None

    normalized_text = (
        text.replace("\xa0", " ")
        .replace("\u2009", " ")
        .replace("\u202F", " ")
    )

    matches: List[Tuple[int, int, int, Decimal]] = []
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
        if prefer_regular:
            priority = 2
            if any(hint in context_lower for hint in REGULAR_CONTEXT_HINTS):
                priority = 0
            elif any(hint in context_lower for hint in CARD_CONTEXT_HINTS):
                priority = 3
            else:
                priority = 1
        if any(hint in context_lower for hint in NEGATIVE_CONTEXT_HINTS):
            priority += 1

        currency_bonus = -1 if ("₽" in context_slice or "руб" in context_lower or "rub" in context_lower or "rur" in context_lower) else 0
        matches.append((priority, currency_bonus, match.start(), price))

    if not matches:
        return None

    matches.sort()
    return matches[0][3]


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


def _score_price_path(path: Sequence[str], *, prefer_regular: bool) -> Optional[int]:
    lowered_path = [segment.lower() for segment in path if segment]
    if not lowered_path:
        return None

    if not any(_contains_any(segment, PRICE_PATH_KEYWORDS) for segment in lowered_path):
        return None

    score = 4 if prefer_regular else 3

    has_without_card = any(_contains_any(segment, CARD_EXCLUSION_HINTS) for segment in lowered_path)
    has_card = any(_contains_any(segment, CARD_PATH_HINTS) for segment in lowered_path)
    has_regular = any(_contains_any(segment, REGULAR_PATH_HINTS) for segment in lowered_path)
    has_current = any("current" in segment for segment in lowered_path)
    has_negative = any(_contains_any(segment, NEGATIVE_PATH_HINTS) for segment in lowered_path)

    if has_regular or has_without_card:
        score = min(score, 0)
    elif has_current:
        score = min(score, 1 if prefer_regular else 1)

    if has_negative:
        score += 1

    if has_card and not has_without_card:
        score += 2 if prefer_regular else 1

    return max(score, 0)


def _collect_price_candidates(data: object, *, prefer_regular: bool) -> List[Tuple[int, int, str, Decimal]]:
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

        score = _score_price_path(path, prefer_regular=prefer_regular)
        if score is None:
            continue

        label = ".".join(path)
        candidates.append((score, len(path), label, price))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates

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
                return price

        element = soup.select_one("[data-test='product-retail-price']")
        if element:
            text = element.get_text(" ", strip=True)
            price = _extract_price_from_text(text, prefer_regular=True)
            if price is not None:
                LOGGER.info("Petrovich: price via [data-test='product-retail-price'] = %s", price)
                return price
            LOGGER.debug("Petrovich data-test price invalid", extra={"url": url})

        meta = soup.select_one("meta[itemprop='price'][content]")
        if meta:
            content = meta.get("content")
            if content:
                price = _extract_price_from_text(content, prefer_regular=True)
                if price is not None:
                    LOGGER.info("Petrovich: price via meta[itemprop='price'] = %s", price)
                    return price
                LOGGER.debug("Petrovich meta price invalid", extra={"url": url})

        attribute_price = self._price_from_data_attributes(soup, url)
        if attribute_price is not None:
            return attribute_price

        next_data_price = self._price_from_next_data(soup, url)
        if next_data_price is not None:
            return next_data_price

        script_price = self._price_from_scripts(soup)
        if script_price is not None:
            return script_price

        for selector in ("[itemprop='offers'] [itemprop='price']", "[class*='price']"):
            for node in soup.select(selector):
                text = node.get("content") or node.get_text(" ", strip=True)
                price = _extract_price_from_text(text, prefer_regular=True)
                if price is None:
                    continue
                if selector == "[itemprop='offers'] [itemprop='price']":
                    LOGGER.info("Petrovich: price via itemprop offers price = %s", price)
                else:
                    LOGGER.info("Petrovich: price via class*='price' = %s", price)
                return price

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

    def _price_from_scripts(self, soup: BeautifulSoup) -> Optional[Decimal]:
        for script in soup.find_all("script"):
            text = script.string or script.text or ""
            if not text.strip():
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue

            candidates = _collect_price_candidates(data, prefer_regular=True)
            if candidates:
                _, _, label, price = candidates[0]
                LOGGER.info("Petrovich: price via inline script %s = %s", label or "price", price)
                return price
        return None

    def _price_from_jsonld(self, product: dict, url: str | None) -> Optional[Decimal]:
        sources: List[Tuple[object, str]] = []
        offers = product.get("offers")
        if isinstance(offers, (dict, list)):
            sources.append((offers, "JSON-LD offers"))
        sources.append((product, "JSON-LD"))

        for data, prefix in sources:
            candidates = _collect_price_candidates(data, prefer_regular=True)
            if not candidates:
                continue
            _, _, label, price = candidates[0]
            label = label or "price"
            LOGGER.info("Petrovich: price via %s.%s = %s", prefix, label, price)
            return price
        LOGGER.debug("Petrovich JSON-LD price not found", extra={"url": url})
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

        search_targets: List[Tuple[object, str]] = []
        if isinstance(product, dict):
            price_section = product.get("price")
            if isinstance(price_section, (dict, list)):
                search_targets.append((price_section, "__NEXT_DATA__.product.price"))
            search_targets.append((product, "__NEXT_DATA__.product"))
        search_targets.append((data, "__NEXT_DATA__"))

        for target, prefix in search_targets:
            candidates = _collect_price_candidates(target, prefer_regular=True)
            if not candidates:
                continue
            _, _, label, price = candidates[0]
            full_label = f"{prefix}.{label}" if label else prefix
            LOGGER.info("Petrovich: price via %s = %s", full_label, price)
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
                    candidate = _extract_price_from_text(f"{attr_lower} {value}", prefer_regular=True)
                    if candidate is None:
                        continue

                    priority = 2
                    if any(token in attr_lower for token in ("retail", "regular", "default", "base", "withoutcard", "nocard", "cardless")):
                        priority = 0
                    elif any(token in attr_lower for token in ("min", "max", "old", "previous", "discount")):
                        priority = 3
                    elif any(token in attr_lower for token in ("card", "bonus", "loyal", "club")) and not any(token in attr_lower for token in ("without", "no_", "nocard", "cardless")):
                        priority = 4

                    current = (priority, candidate, attr_lower)
                    if best is None or current < best:
                        best = current

        if best is not None:
            _, price, attr_name = best
            LOGGER.info("Petrovich: price via data attribute %s = %s", attr_name, price)
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


__all__ = ["PetrovichParser"]
