"""Parser implementation for whitehills.ru."""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import time
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Optional

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from pricing.config import settings

from .base import BaseParser, PriceNotFoundError, ProductSnapshot, ScraperError

LOGGER = logging.getLogger(__name__)

THIN_SPACES = ("\xa0", "\u2009", "\u202F")


def _norm_price(txt: str) -> Decimal:
    t = txt or ""
    for sp in THIN_SPACES:
        t = t.replace(sp, " ")
    t = re.sub(r"(руб\.?|₽)", "", t, flags=re.I)
    t = re.sub(r"\s+", "", t).replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", t)
    if not match:
        raise ValueError(f"no number in: {txt!r}")
    return Decimal(match.group(0))


def _ensure_tmp_dir() -> str:
    directory = "/app/tmp"
    try:
        pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
    except Exception:
        directory = "/tmp"
        pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
    return directory


async def _price_from_dom(page, logger) -> Decimal | None:
    for selector in (".cookie", ".cookie-agreement", ".cookie__button", ".agree", "button[class*='cookie']"):
        try:
            button = page.locator(selector)
            if await button.first.is_visible():
                await button.first.click(timeout=1000)
        except Exception:
            pass

    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(600)

    candidates = [
        ".values_wrapper .price_value",
        ".price_value",
        ".product__price .price_value",
        ".prices_block .price_value",
        "span.price_value",
    ]

    texts: list[str] = []
    for css in candidates:
        try:
            locator = page.locator(css)
            count = await locator.count()
            if count == 0:
                continue
            for index in range(count):
                element = locator.nth(index)
                if not await element.is_visible():
                    continue
                raw_text = (await element.text_content()) or ""
                raw_text = raw_text.strip()
                if raw_text:
                    texts.append(raw_text)
            if texts:
                break
        except Exception:
            continue

    logger.info(
        "whitehills: found %d .price_value nodes; texts=%s",
        len(texts),
        [text[:48] for text in texts],
    )

    strategies = (
        lambda seq: seq[-1:],
        lambda seq: sorted(seq, key=lambda value: (len(value), value)),
    )
    for strategy in strategies:
        try:
            sequence = list(strategy(list(texts)))
        except Exception:
            continue
        for text in sequence:
            try:
                return _norm_price(text)
            except Exception:
                continue

    return None


def _price_from_jsonld(html_or_texts, logger) -> Decimal | None:
    if isinstance(html_or_texts, str):
        json_texts = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html_or_texts,
            flags=re.I | re.S,
        )
    else:
        json_texts = [text for text in html_or_texts if text]

    for raw_text in json_texts:
        try:
            data = json.loads(raw_text)
        except Exception:
            continue

        objects = data if isinstance(data, list) else [data]
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            offers = obj.get("offers")
            if not offers and obj.get("@type") == "Product":
                offers = obj.get("offers")
            if not offers:
                continue
            offers_list = offers if isinstance(offers, list) else [offers]
            for offer in offers_list:
                if not isinstance(offer, dict):
                    continue
                price_value = offer.get("price")
                if price_value is None:
                    continue
                try:
                    return _norm_price(str(price_value))
                except Exception:
                    continue
    return None


class WhiteHillsParser(BaseParser):
    """Parser for WhiteHills store."""

    @property
    def logger(self) -> logging.Logger:
        return LOGGER

    @staticmethod
    def _to_decimal(text: str) -> Decimal:
        return _norm_price(text)

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        price: Decimal | None = None

        settings_obj = getattr(self, "settings", settings)

        try:  # pragma: no cover - requires Playwright
            async with async_playwright() as playwright_ctx:
                browser = await playwright_ctx.chromium.launch(
                    headless=getattr(settings_obj, "playwright_headless", True),
                    slow_mo=getattr(settings_obj, "playwright_slow_mo", 0),
                    args=(os.environ.get("PW_LAUNCH_ARGS") or "").split() or None,
                )
                try:
                    context = None
                    context = await browser.new_context(
                        locale="ru-RU",
                        timezone_id=os.environ.get("PLAYWRIGHT_TZ", "Europe/Moscow"),
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36"
                        ),
                        viewport={"width": 1366, "height": 900},
                    )
                    try:
                        async def _route_handler(route):
                            if route.request.resource_type in {"image", "font"}:
                                await route.abort()
                            else:
                                await route.continue_()

                        await context.route("**/*", _route_handler)
                        page = await context.new_page()
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_load_state("networkidle")

                        price = await _price_from_dom(page, self.logger)
                        if price is not None:
                            self.logger.info("whitehills: price via playwright = %s", price)
                            return ProductSnapshot(
                                url=url,
                                price=price,
                                currency="RUB",
                                title=None,
                                sku=None,
                                variant_key=variant,
                                payload=None,
                            )

                        try:
                            json_texts = await page.evaluate(
                                """
                                () => Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                                      .map(s => s.textContent || '')
                                """
                            )
                        except Exception:
                            json_texts = []

                        price = _price_from_jsonld(json_texts, self.logger)
                        if price is not None:
                            self.logger.info("whitehills: price via jsonld = %s", price)
                            return ProductSnapshot(
                                url=url,
                                price=price,
                                currency="RUB",
                                title=None,
                                sku=None,
                                variant_key=variant,
                                payload=None,
                            )

                        try:
                            tmp_dir = _ensure_tmp_dir()
                            timestamp = int(time.time())
                            screenshot_path = os.path.join(tmp_dir, f"whitehills_{timestamp}.png")
                            html_path = os.path.join(tmp_dir, f"whitehills_{timestamp}.html")
                            block_path = os.path.join(tmp_dir, f"whitehills_priceblock_{timestamp}.html")
                            await page.screenshot(path=screenshot_path, full_page=True)
                            content = await page.content()
                            with open(html_path, "w", encoding="utf-8") as handle:
                                handle.write(content)
                            try:
                                block_html = await page.evaluate(
                                    """
                                    () => {
                                      const q = ['.prices_block', '.price_matrix_block', '.product__price', '.values_wrapper'];
                                      for (const sel of q) {
                                        const el = document.querySelector(sel);
                                        if (el) return el.outerHTML;
                                      }
                                      return '';
                                    }
                                    """
                                )
                                if block_html:
                                    with open(block_path, "w", encoding="utf-8") as handle:
                                        handle.write(block_html)
                            except Exception:
                                pass
                            self.logger.warning(
                                "whitehills: debug dump saved to %s; screenshot=%s",
                                html_path,
                                screenshot_path,
                            )
                        except Exception as dump_exc:
                            self.logger.info("whitehills: debug dump error: %s", dump_exc)
                    finally:
                        if context is not None:
                            await context.close()
                finally:
                    await browser.close()
        except Exception as exc:  # pragma: no cover - optional dependency or runtime issues
            self.logger.info("whitehills: playwright error: %s", exc)

        try:
            html = await self.fetch_html(url)
            price = _price_from_jsonld(html, self.logger)
            if price is None:
                match = re.search(
                    r'<span[^>]*class=["\']price_value["\'][^>]*>(.*?)</span>',
                    html,
                    flags=re.I | re.S,
                )
                if match:
                    price = _norm_price(match.group(1))
            if price is not None:
                self.logger.info("whitehills: price via static = %s", price)
                return ProductSnapshot(
                    url=url,
                    price=price,
                    currency="RUB",
                    title=None,
                    sku=None,
                    variant_key=variant,
                    payload=None,
                )
        except Exception:
            pass

        self.logger.warning("whitehills: price not found")
        raise ScraperError("Price not found on WhiteHills product page")

    def parse_price(self, html: str, url: str | None = None) -> Decimal:
        soup = BeautifulSoup(html, "lxml")
        price = self._parse_price_from_soup(soup, url=url)
        if price is None:
            self.logger.warning("WhiteHills price not found", extra={"url": url})
            raise PriceNotFoundError("Price not found on WhiteHills product page")
        return price

    async def fetch_category(self, url: str) -> list[ProductSnapshot]:
        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        items: list[ProductSnapshot] = []
        for card in soup.select(".collection__item, .products-list__item"):
            link = card.select_one("a")
            price_node = card.select_one(".price, .product__price")
            if not link or not price_node:
                continue
            href = link.get("href") or ""
            try:
                price_value = self.normalize_price(price_node.get_text())
            except ValueError:
                self.logger.debug("WhiteHills category price parse failed", extra={"url": url})
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

    def _parse_price_from_soup(
        self,
        soup: BeautifulSoup,
        *,
        url: str | None = None,
        jsonld_product: Optional[dict[str, Any]] = None,
    ) -> Decimal | None:
        product = jsonld_product or self._find_jsonld_product(soup)
        if product:
            price = self._price_from_jsonld_product(product)
            if price is not None:
                self.logger.info("whitehills: price via jsonld = %s", price, extra={"url": url})
                return price
        price = self._price_from_static_dom(soup, url=url)
        return price

    def _find_jsonld_product(self, soup: BeautifulSoup) -> Optional[dict[str, Any]]:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            text = script.string or script.text or ""
            if not text.strip():
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            for candidate in self._iter_dicts(data):
                if self._is_product_type(candidate.get("@type")):
                    return candidate
        return None

    def _price_from_jsonld_product(self, product: dict[str, Any]) -> Decimal | None:
        offers = product.get("offers")
        offer: dict[str, Any] | None
        if isinstance(offers, list):
            offer = next((item for item in offers if isinstance(item, dict)), None)
        elif isinstance(offers, dict):
            offer = offers
        else:
            offer = None
        if not offer:
            return None
        for key in ("price", "priceValue", "lowPrice", "highPrice", "currentPrice", "value", "amount"):
            if key in offer and offer[key] is not None:
                try:
                    return self._to_decimal(str(offer[key]))
                except Exception:
                    continue
        return None

    def _price_from_static_dom(self, soup: BeautifulSoup, url: str | None = None) -> Decimal | None:
        first_pass_selectors = ("span.price_value", ".values_wrapper .price_value")
        for selector in first_pass_selectors:
            element = soup.select_one(selector)
            if element and element.get_text(strip=True):
                text = element.get_text(" ", strip=True)
                try:
                    price = self._to_decimal(text)
                except Exception:
                    continue
                self.logger.info("whitehills: price via dom = %s", price, extra={"url": url})
                return price

        meta = soup.select_one("meta[itemprop='price']")
        if meta and meta.get("content"):
            try:
                price = self._to_decimal(meta["content"])
                self.logger.info("whitehills: price via dom = %s", price, extra={"url": url})
                return price
            except Exception:
                pass

        for selector in ("[itemprop='offers'] [itemprop='price']", "[class*='price']"):
            element = soup.select_one(selector)
            if not element:
                continue
            text = element.get_text(" ", strip=True)
            if not text:
                continue
            try:
                price = self._to_decimal(text)
            except Exception:
                continue
            self.logger.info("whitehills: price via dom = %s", price, extra={"url": url})
            return price

        for script in soup.find_all("script"):
            text = script.string or script.text or ""
            if not text:
                continue
            match = re.search(r"\"(?:price|currentPrice|amount|value|priceValue)\"\s*:\s*\"?(\d+(?:[.,]\d{1,2})?)\"?", text)
            if not match:
                continue
            try:
                price = self._to_decimal(match.group(1))
            except Exception:
                continue
            self.logger.info("whitehills: price via dom = %s", price, extra={"url": url})
            return price
        return None

    def _iter_dicts(self, data: Any) -> Iterable[dict[str, Any]]:
        if isinstance(data, dict):
            yield data
            for value in data.values():
                yield from self._iter_dicts(value)
        elif isinstance(data, list):
            for item in data:
                yield from self._iter_dicts(item)

    def _is_product_type(self, value: Any) -> bool:
        if isinstance(value, str):
            return value.lower() == "product"
        if isinstance(value, Iterable):
            return any(isinstance(item, str) and item.lower() == "product" for item in value)
        return False


def to_decimal(text: str) -> Decimal:
    """Backward-compatible helper for tests and other modules."""

    return WhiteHillsParser._to_decimal(text)


__all__ = ["WhiteHillsParser"]
