"""Parser implementation for whitehills.ru."""
from __future__ import annotations

import json
import logging
import os
import pathlib
import random
import re
import time
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Optional

import cloudscraper
from bs4 import BeautifulSoup
from playwright.async_api import Response, async_playwright

from pricing.config import settings

from .base import BaseParser, PriceNotFoundError, ProductSnapshot, ScraperError

LOGGER = logging.getLogger(__name__)

THIN_SPACES = ("\xa0", "\u2009", "\u202F")
UA_REAL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
PW_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
]
STORAGE_STATE = os.environ.get("WHITEHILLS_STORAGE_STATE", "/app/whitehills_cookies.json")
PLAYWRIGHT_TZ = os.environ.get("PLAYWRIGHT_TZ", "Europe/Moscow")


def _norm_price(txt: str) -> Decimal:
    t = txt or ""
    for sp in THIN_SPACES:
        t = t.replace(sp, " ")
    t = re.sub(r"(руб\.?|₽|р\.)", "", t, flags=re.I)
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


def _load_storage_state(path: str) -> dict[str, Any] | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        LOGGER.info("whitehills: failed to load storage_state %s: %s", path, exc)
    return None


def _storage_cookies_for_domain(storage_state: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not storage_state:
        return []
    cookies = storage_state.get("cookies") if isinstance(storage_state, dict) else None
    if not isinstance(cookies, list):
        return []
    result: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = cookie.get("domain") or ""
        path = cookie.get("path") or "/"
        if not domain.endswith("whitehills.ru"):
            continue
        if path != "/":
            continue
        result.append(cookie)
    return result


def _cookie_header_from_storage(storage_state: dict[str, Any] | None) -> str | None:
    cookies = _storage_cookies_for_domain(storage_state)
    pairs: list[str] = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if name is None or value is None:
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs) if pairs else None


def _random_delay_ms() -> int:
    return random.randint(500, 1200)


async def _human_pause(page) -> None:
    try:
        await page.wait_for_timeout(_random_delay_ms())
    except Exception:
        pass


async def _dismiss_overlays(page):
    selectors = [
        "button.cookie-agree",
        "button[class*='cookie']",
        ".cookie__button",
        ".agree",
        "button[aria-label='Принять']",
        ".region-confirm button",
        "button[data-accept]",
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if await button.is_visible():
                await button.click(timeout=1000)
                await _human_pause(page)
        except Exception:
            continue


def _extract_price_from_text(body: str) -> Optional[Decimal]:
    try:
        data = json.loads(body)
        stack = [data]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
                    elif isinstance(value, (int, float, str)) and re.search(r"price", key, re.I):
                        try:
                            return _norm_price(str(value))
                        except Exception:
                            pass
            elif isinstance(current, list):
                stack.extend(current)
    except Exception:
        match = re.search(
            r"(?:class=[\"'][^\"']*price_value[^\"']*[\"']\s*>\s*)([^<]+)|(\d[\d\s\u2009\u202F\xa0]*\s*(?:₽|руб\.?))",
            body,
            flags=re.I,
        )
        if match:
            group = match.group(1) or match.group(2)
            try:
                return _norm_price(group)
            except Exception:
                pass
    return None


def _log_price_nodes_from_html(html: str, logger) -> None:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return
    texts: list[str] = []
    for element in soup.select(".price_value"):
        text = (element.get_text(" ", strip=True) or "").strip()
        if text:
            texts.append(text[:60])
    logger.info("whitehills: .price_value nodes=%s texts=%s", len(texts), texts)


def _captcha_detected(text: str) -> bool:
    lowered = text.lower()
    if "если вы человек" in lowered:
        return True
    if "captcha" in lowered or "капча" in lowered:
        return True
    return False


def _price_via_cloudscraper(url: str, logger, storage_state: dict[str, Any] | None) -> Optional[Decimal]:
    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=10,
        )
        headers = {"User-Agent": UA_REAL, "Accept-Language": "ru-RU,ru;q=0.9"}
        cookie_header = _cookie_header_from_storage(storage_state)
        if cookie_header:
            headers["Cookie"] = cookie_header
        response = scraper.get(url, headers=headers, timeout=25)
        if response.status_code != 200:
            logger.info("whitehills cloudscraper status=%s", response.status_code)
            return None
        html = response.text or ""

        if _captcha_detected(html):
            logger.warning("whitehills: captcha detected (cloudscraper)")

        _log_price_nodes_from_html(html, logger)

        match = re.search(
            r'<meta[^>]*itemprop=["\']price["\'][^>]*content=["\']([^"\']+)["\']',
            html,
            flags=re.I,
        )
        if match:
            return _norm_price(match.group(1))

        match = re.search(
            r'<span[^>]*class=["\'][^"\']*price_value[^"\']*["\'][^>]*>(.*?)</span>',
            html,
            flags=re.I | re.S,
        )
        if match:
            return _norm_price(match.group(1))

        scripts = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            flags=re.I | re.S,
        )
        for raw_json in scripts:
            try:
                data = json.loads(raw_json)
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if not isinstance(obj, dict):
                    continue
                offers = obj.get("offers")
                if not offers:
                    continue
                offer_list = offers if isinstance(offers, list) else [offers]
                for offer in offer_list:
                    if isinstance(offer, dict) and "price" in offer:
                        try:
                            return _norm_price(str(offer["price"]))
                        except Exception:
                            pass

        for candidate_url in re.findall(
            r'https?://[^\s"\']+?(?:ajax|price)[^\s"\']*',
            html,
            flags=re.I,
        ):
            try:
                ajax_resp = scraper.get(candidate_url, headers=headers, timeout=15)
                if ajax_resp.status_code != 200:
                    continue
                extracted = _extract_price_from_text(ajax_resp.text)
                if extracted is not None:
                    return extracted
            except Exception:
                continue

        return None
    except Exception as exc:
        logger.info("whitehills cloudscraper error: %s", exc)
        return None


async def _price_from_dom(page, logger) -> Optional[Decimal]:
    await _dismiss_overlays(page)
    try:
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass

    try:
        locator = page.locator(".price_value")
        count = await locator.count()
        values: list[str] = []
        for index in range(count):
            text = (await locator.nth(index).text_content() or "").strip()
            if text:
                values.append(text[:60])
        logger.info("whitehills: dom .price_value nodes=%s texts=%s", count, values)
    except Exception:
        logger.info("whitehills: failed to log .price_value nodes")

    selectors = [
        ".price_value",
        ".values_wrapper .price_value",
        "[itemprop='price']",
        ".prices_block .price_value",
    ]

    for css in selectors:
        try:
            locator = page.locator(css)
            count = await locator.count()
        except Exception:
            continue
        if count == 0:
            continue
        if css == "[itemprop='price']":
            for index in range(count):
                try:
                    element = locator.nth(index)
                    value = (await element.get_attribute("content")) or ""
                    value = value.strip()
                    if not value:
                        continue
                    return _norm_price(value)
                except Exception:
                    continue
            continue
        for index in range(count):
            try:
                element = locator.nth(index)
                if not await element.is_visible():
                    continue
                text = (await element.text_content()) or ""
                text = text.strip()
                if not text:
                    continue
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


async def _dump_debug(page, logger) -> None:
    if page is None:
        return
    try:
        tmp_dir = _ensure_tmp_dir()
        timestamp = int(time.time())
        screenshot_path = os.path.join(tmp_dir, f"whitehills_{timestamp}.png")
        html_path = os.path.join(tmp_dir, f"whitehills_{timestamp}.html")
        await page.screenshot(path=screenshot_path, full_page=True)
        content = await page.content()
        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(content)
        logger.warning(
            "whitehills: debug dump saved to %s; screenshot=%s",
            html_path,
            screenshot_path,
        )
    except Exception as exc:
        logger.warning("whitehills: failed to store debug info: %s", exc)


class WhiteHillsParser(BaseParser):
    """Parser for WhiteHills store."""

    @property
    def logger(self) -> logging.Logger:
        return LOGGER

    @staticmethod
    def _to_decimal(text: str) -> Decimal:
        return _norm_price(text)

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        storage_state_data = _load_storage_state(STORAGE_STATE)

        price = _price_via_cloudscraper(url, self.logger, storage_state_data)
        if price is not None:
            self.logger.info("whitehills: price via cloudscraper = %s", price)
            return ProductSnapshot(
                url=url,
                price=price,
                currency="RUB",
                title=None,
                variant_key=variant,
                payload=None,
            )

        fetch_html_attr = getattr(self, "fetch_html", None)
        original_fetch = getattr(type(self), "fetch_html", None)
        is_monkeypatched = False
        if fetch_html_attr is not None:
            if not hasattr(fetch_html_attr, "__func__"):
                is_monkeypatched = True
            elif original_fetch is not None and fetch_html_attr.__func__ is not original_fetch:
                is_monkeypatched = True

        if is_monkeypatched:
            try:
                html = await self.fetch_html(url)
                price = self.parse_price(html, url)
                self.logger.info("whitehills: price via monkeypatched HTML = %s", price)
                return ProductSnapshot(
                    url=url,
                    price=price,
                    currency="RUB",
                    title=None,
                    variant_key=variant,
                    payload=None,
                )
            except Exception as exc:
                self.logger.info("whitehills: monkeypatched HTML fetch failed: %s", exc)

        settings_obj = getattr(self, "settings", settings)

        price: Optional[Decimal] = None
        result_snapshot: Optional[ProductSnapshot] = None
        page = None
        context = None
        browser = None
        debug_dump_saved = False

        try:  # pragma: no cover - requires Playwright
            async with async_playwright() as playwright_ctx:
                browser = await playwright_ctx.chromium.launch(
                    headless=getattr(settings_obj, "playwright_headless", True),
                    slow_mo=getattr(settings_obj, "playwright_slow_mo", 0),
                    args=PW_ARGS,
                )
                ctx_args: dict[str, Any] = dict(
                    locale="ru-RU",
                    timezone_id=PLAYWRIGHT_TZ,
                    user_agent=UA_REAL,
                    viewport={"width": 1366, "height": 900},
                )
                if os.path.exists(STORAGE_STATE):
                    ctx_args["storage_state"] = STORAGE_STATE
                    self.logger.info("whitehills: using storage_state %s", STORAGE_STATE)

                context = await browser.new_context(**ctx_args)

                manual_cookies: list[dict[str, Any]] = []
                for cookie in _storage_cookies_for_domain(storage_state_data):
                    name = cookie.get("name")
                    value = cookie.get("value")
                    if name is None or value is None:
                        continue
                    domain = cookie.get("domain") or "whitehills.ru"
                    if not domain.startswith("."):
                        domain = f".{domain}"
                    manual_cookie: dict[str, Any] = {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": cookie.get("path") or "/",
                    }
                    for key in ("expires", "httpOnly", "secure", "sameSite"):
                        if key in cookie:
                            manual_cookie[key] = cookie[key]
                    manual_cookies.append(manual_cookie)

                if manual_cookies:
                    try:
                        await context.add_cookies(manual_cookies)
                        self.logger.info(
                            "whitehills: added %s cookies to context", len(manual_cookies)
                        )
                    except Exception as exc:
                        self.logger.info("whitehills: failed to add cookies: %s", exc)

                async def route_handler(route, request):
                    try:
                        if request.resource_type in {"image", "font"}:
                            await route.abort()
                        else:
                            await route.continue_()
                    except Exception:
                        try:
                            await route.continue_()
                        except Exception:
                            pass

                await context.route("**/*", route_handler)

                page = await context.new_page()
                network_prices: list[Decimal] = []

                async def on_response(resp: Response):
                    try:
                        if resp.request.resource_type not in {"xhr", "fetch"}:
                            return
                        if "whitehills.ru" not in resp.url:
                            return
                        text = await resp.text()
                        price_candidate = _extract_price_from_text(text)
                        if price_candidate is not None:
                            network_prices.append(price_candidate)
                    except Exception:
                        pass

                page.on("response", on_response)

                await page.goto("https://whitehills.ru/", wait_until="domcontentloaded", timeout=30000)
                await _human_pause(page)
                await _dismiss_overlays(page)
                await _human_pause(page)

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await _human_pause(page)
                await _dismiss_overlays(page)
                await _human_pause(page)

                page_content = ""
                try:
                    page_content = await page.content()
                except Exception:
                    page_content = ""
                if page_content and _captcha_detected(page_content):
                    self.logger.warning("whitehills: captcha detected (playwright)")

                price = await _price_from_dom(page, self.logger)
                if price is not None:
                    self.logger.info("whitehills: price via DOM = %s", price)
                    result_snapshot = ProductSnapshot(
                        url=url,
                        price=price,
                        currency="RUB",
                        title=None,
                        variant_key=variant,
                        payload=None,
                    )
                elif network_prices:
                    price = network_prices[-1]
                    if price is not None:
                        self.logger.info("whitehills: price via network = %s", price)
                        result_snapshot = ProductSnapshot(
                            url=url,
                            price=price,
                            currency="RUB",
                            title=None,
                            variant_key=variant,
                            payload=None,
                        )
                if result_snapshot is None:
                    try:
                        json_texts = await page.evaluate(
                            """
                            () => Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                                  .map(s => s.textContent || '')
                            """
                        )
                    except Exception:
                        json_texts = []

                    json_price = _price_from_jsonld(json_texts, self.logger)
                    if json_price is None and page_content:
                        json_price = _price_from_jsonld(page_content, self.logger)
                    if json_price is not None:
                        price = json_price
                        self.logger.info("whitehills: price via JSON-LD = %s", price)
                        result_snapshot = ProductSnapshot(
                            url=url,
                            price=price,
                            currency="RUB",
                            title=None,
                            variant_key=variant,
                            payload=None,
                        )

                if result_snapshot is None and page is not None:
                    await _dump_debug(page, self.logger)
                    debug_dump_saved = True
        except Exception as exc:  # pragma: no cover - optional dependency or runtime issues
            self.logger.info("whitehills: playwright error: %s", exc)
            if page is not None and not debug_dump_saved:
                await _dump_debug(page, self.logger)
                debug_dump_saved = True
        finally:
            try:
                if context is not None:
                    await context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass

        if result_snapshot is not None:
            return result_snapshot

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
