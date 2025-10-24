"""Parser implementation for whitehills.ru."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Optional

from bs4 import BeautifulSoup

from pricing.config import settings

from .base import BaseParser, PriceNotFoundError, ProductSnapshot

LOGGER = logging.getLogger(__name__)

_THIN = ("\xa0", "\u2009", "\u202F")
_PLAYWRIGHT_WAIT_SELECTORS = (
    "span.price_value",
    ".values_wrapper .price_value",
    "[itemprop='offers'] [itemprop='price']",
)
_PRICE_JSON_KEYS = {
    "price",
    "price_value",
    "priceValue",
    "current",
    "currentPrice",
    "value",
    "amount",
    "lowPrice",
    "highPrice",
}

def to_decimal(text: str) -> Decimal:
    t = (text or "")
    for sp in _THIN:
        t = t.replace(sp, " ")
    t = re.sub(r"[^\d.,\s]", "", t)
    t = re.sub(r"\s+", "", t).replace(",", ".")
    match = re.search(r"\d+(?:\.\d{1,2})?", t)
    if not match:
        raise ValueError(f"no numeric in: {text!r}")
    return Decimal(match.group(0))


class WhiteHillsParser(BaseParser):
    """Parser for WhiteHills store."""

    async def fetch_product(self, url: str, *, variant: Optional[str] = None) -> ProductSnapshot:
        price: Optional[Decimal] = None

        try:  # pragma: no cover - optional dependency
            from playwright.async_api import (  # type: ignore import-not-found
                TimeoutError as PlaywrightTimeoutError,
                async_playwright,
            )
        except Exception:
            LOGGER.info("whitehills: playwright unavailable", extra={"url": url})
        else:  # pragma: no cover - requires browser
            browser = None
            context = None
            page = None
            try:
                async with async_playwright() as playwright_ctx:
                    launch_args = shlex.split(os.environ.get("PW_LAUNCH_ARGS", ""))
                    browser = await playwright_ctx.chromium.launch(
                        headless=settings.playwright_headless,
                        args=launch_args or None,
                    )
                    headers = self._build_headers().copy()
                    user_agent = headers.pop("User-Agent", None) or self._choose_user_agent()
                    context = await browser.new_context(user_agent=user_agent, extra_http_headers=headers)
                    page = await context.new_page()
                    await page.goto(url, wait_until="networkidle")
                    await page.wait_for_timeout(2000)

                    try:
                        await page.wait_for_selector(
                            "script[type='application/ld+json'], .price_value",
                            timeout=8000,
                        )
                    except PlaywrightTimeoutError:
                        LOGGER.warning("WhiteHills price element not found", extra={"url": url})

                    scripts = await page.query_selector_all("script[type='application/ld+json']")
                    for script in scripts:
                        try:
                            raw_json = await script.inner_text()
                            if not raw_json:
                                continue
                            data = json.loads(raw_json)
                        except Exception:
                            continue

                        items: Iterable[Any]
                        if isinstance(data, list):
                            items = data
                        else:
                            items = [data]
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            offers = item.get("offers")
                            offer_items: Iterable[Any]
                            if isinstance(offers, list):
                                offer_items = offers
                            elif offers is None:
                                offer_items = []
                            else:
                                offer_items = [offers]
                            for offer in offer_items:
                                if isinstance(offer, dict) and "price" in offer:
                                    try:
                                        price_value = float(offer["price"])
                                    except (TypeError, ValueError):
                                        continue
                                    price = Decimal(str(price_value))
                                    break
                            if price is not None:
                                break
                        if price is not None:
                            break

                    if price is None:
                        try:
                            price_el = await page.query_selector(".price_value")
                        except PlaywrightTimeoutError:
                            price_el = None
                        if price_el is None:
                            LOGGER.warning("WhiteHills price element not found", extra={"url": url})
                        else:
                            try:
                                text = await price_el.inner_text()
                            except Exception:
                                text = ""
                            text = (text or "").strip()
                            text = (
                                text.replace(" ", "")
                                .replace("\xa0", "")
                                .replace("₽", "")
                                .replace(",", ".")
                            )
                            if text:
                                try:
                                    price_value = float(text)
                                except ValueError:
                                    price_value = None
                                if price_value is not None:
                                    price = Decimal(str(price_value))
                                else:
                                    LOGGER.warning("WhiteHills price element not found", extra={"url": url})
                            else:
                                LOGGER.warning("WhiteHills price element not found", extra={"url": url})
            except Exception as exc:
                LOGGER.warning("WhiteHills playwright extract error: %s", exc, extra={"url": url})
            finally:
                try:
                    if page is not None:
                        await page.close()
                except Exception:
                    pass
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

        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        jsonld_product = self._find_jsonld_product(soup)

        if price is None:
            price = self._parse_price_from_soup(soup, url=url, jsonld_product=jsonld_product)
            if price is None:
                LOGGER.warning("WhiteHills price not found", extra={"url": url})
                raise PriceNotFoundError("Price not found on WhiteHills product page")

        title: Optional[str] = None
        sku: Optional[str] = None
        variant_key = variant

        if jsonld_product:
            title = jsonld_product.get("name") or jsonld_product.get("title") or title
            sku = jsonld_product.get("sku") or jsonld_product.get("mpn") or sku
            if variant_key is None:
                variant_key = jsonld_product.get("variant")

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
        price = self._parse_price_from_soup(soup, url=url)
        if price is None:
            LOGGER.warning("WhiteHills price not found", extra={"url": url})
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

    async def _fetch_price_playwright(self, url: str) -> Decimal | None:
        """Return price extracted with Playwright or ``None`` when unavailable."""

        try:  # pragma: no cover - optional dependency
            from playwright.async_api import (  # type: ignore import-not-found
                TimeoutError as PlaywrightTimeoutError,
                async_playwright,
            )
        except Exception:
            LOGGER.info("whitehills: playwright extract returned None", extra={"url": url})
            return None

        result: Decimal | None = None
        browser = None
        context = None
        page = None
        price_candidates: list[str] = []

        def _capture_price_candidate(value: Any) -> bool:
            text = None
            if isinstance(value, (int, float, Decimal)):
                text = str(value)
            elif isinstance(value, str):
                text = value
            if text and any(char.isdigit() for char in text):
                price_candidates.append(text)
                return True
            return False

        def _scan_json(data: Any) -> bool:
            if isinstance(data, dict):
                for key, val in data.items():
                    if key in _PRICE_JSON_KEYS and _capture_price_candidate(val):
                        return True
                    if _scan_json(val):
                        return True
            elif isinstance(data, list):
                for item in data:
                    if _scan_json(item):
                        return True
            return False

        async def _handle_response(response: Any) -> None:
            if price_candidates:
                return
            try:
                url_lower = response.url.lower()
            except Exception:
                return
            if not any(token in url_lower for token in ("price", "catalog", "product", "offer")):
                return
            try:
                headers = response.headers
            except Exception:
                headers = {}
            content_type = "" if headers is None else headers.get("content-type", "")
            if "application/json" not in (content_type or ""):
                return
            try:
                payload = await response.json()
            except Exception:
                return
            _scan_json(payload)

        try:  # pragma: no cover - requires browser
            async with async_playwright() as playwright_ctx:
                launch_args = shlex.split(os.environ.get("PW_LAUNCH_ARGS", ""))
                browser = await playwright_ctx.chromium.launch(
                    headless=settings.playwright_headless,
                    args=launch_args or None,
                )

                headers = self._build_headers().copy()
                user_agent = headers.pop("User-Agent", None) or self._choose_user_agent()
                context = await browser.new_context(
                    user_agent=user_agent,
                    extra_http_headers=headers or None,
                )

                page = await context.new_page()
                page.on("response", _handle_response)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except PlaywrightTimeoutError:
                    pass

                if price_candidates:
                    try:
                        result = to_decimal(price_candidates[0])
                        LOGGER.info(
                            "whitehills: price via playwright-xhr = %s",
                            result,
                            extra={"url": url},
                        )
                        return result
                    except ValueError:
                        price_candidates.clear()

                selector_union = ", ".join(_PLAYWRIGHT_WAIT_SELECTORS)
                try:
                    await page.wait_for_selector(selector_union, timeout=12000)
                except PlaywrightTimeoutError:
                    pass

                jsonld_text = await page.evaluate(
                    r"""
                    () => {
                      const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
                      for (const s of scripts) {
                        try {
                          const parsed = JSON.parse(s.textContent || "{}");
                          const arr = Array.isArray(parsed) ? parsed : [parsed];
                          for (const it of arr) {
                            if (!it) continue;
                            const type = it['@type'];
                            if (type === 'Product'
                                || (typeof type === 'string' && type.toLowerCase().includes('product'))
                                || (Array.isArray(type) && type.includes && type.includes('Product'))) {
                              const offers = it.offers;
                              if (!offers) continue;
                              if (Array.isArray(offers)) {
                                const first = offers.find(item => item && typeof item === 'object');
                                if (first && (first.price || first.priceValue || first.lowPrice || first.currentPrice)) {
                                  return String(first.price || first.priceValue || first.lowPrice || first.currentPrice);
                                }
                              } else if (typeof offers === 'object') {
                                if (offers.price || offers.priceValue || offers.lowPrice || offers.currentPrice) {
                                  return String(offers.price || offers.priceValue || offers.lowPrice || offers.currentPrice);
                                }
                              }
                            }
                          }
                        } catch (e) {}
                      }
                      return null;
                    }
                    """
                )

                if isinstance(jsonld_text, str) and jsonld_text.strip():
                    try:
                        result = to_decimal(jsonld_text)
                        LOGGER.info(
                            "whitehills: price via playwright-jsonld = %s",
                            result,
                            extra={"url": url},
                        )
                        return result
                    except ValueError:
                        result = None

                dom_text = await page.evaluate(
                    r"""
                    () => {
                      const pick = sel => {
                        const el = document.querySelector(sel);
                        return el && el.textContent ? el.textContent : null;
                      };
                      const candidates = [
                        'span.price_value',
                        '.values_wrapper .price_value',
                        "[itemprop='offers'] [itemprop='price']",
                        "[class*='price']",
                      ];
                      for (const selector of candidates) {
                        const value = pick(selector);
                        if (value && /\d/.test(value)) return value;
                      }
                      const meta = document.querySelector('meta[itemprop="price"]');
                      if (meta && meta.content) return meta.content;
                      for (const sc of document.scripts) {
                        const txt = sc.textContent || "";
                        const match = txt.match(/"(?:price|currentPrice|amount|value|priceValue)"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)"?/);
                        if (match) return match[1];
                      }
                      return null;
                    }
                    """
                )

                if isinstance(dom_text, str) and dom_text.strip():
                    try:
                        result = to_decimal(dom_text)
                        LOGGER.info(
                            "whitehills: price via playwright-dom = %s",
                            result,
                            extra={"url": url},
                        )
                        return result
                    except ValueError:
                        result = None
        except Exception:
            result = None
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

        LOGGER.info("whitehills: playwright extract returned None", extra={"url": url})
        return result

    # ------------------------------------------------------------------
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
                LOGGER.info("whitehills: price via jsonld = %s", price, extra={"url": url})
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
        price_value = offer.get("price")
        if price_value is None:
            price_value = offer.get("priceValue")
        if price_value is None:
            return None
        try:
            return to_decimal(str(price_value))
        except ValueError:
            return None

    def _price_from_static_dom(self, soup: BeautifulSoup, url: str | None = None) -> Decimal | None:
        first_pass_selectors = ("span.price_value", ".values_wrapper .price_value")
        for selector in first_pass_selectors:
            element = soup.select_one(selector)
            if element and element.get_text(strip=True):
                text = element.get_text(" ", strip=True)
                try:
                    price = to_decimal(text)
                except ValueError:
                    continue
                LOGGER.info("whitehills: price via dom = %s", price, extra={"url": url})
                return price

        meta = soup.select_one("meta[itemprop='price']")
        if meta and meta.get("content"):
            try:
                price = to_decimal(meta["content"])
                LOGGER.info("whitehills: price via dom = %s", price, extra={"url": url})
                return price
            except ValueError:
                pass

        for selector in ("[itemprop='offers'] [itemprop='price']", "[class*='price']"):
            element = soup.select_one(selector)
            if not element:
                continue
            text = element.get_text(" ", strip=True)
            if not text:
                continue
            try:
                price = to_decimal(text)
            except ValueError:
                continue
            LOGGER.info("whitehills: price via dom = %s", price, extra={"url": url})
            return price

        for script in soup.find_all("script"):
            text = script.string or script.text or ""
            if not text:
                continue
            match = re.search(r"\"(?:price|currentPrice|amount|value|priceValue)\"\s*:\s*\"?(\d+(?:[.,]\d{1,2})?)\"?", text)
            if not match:
                continue
            try:
                price = to_decimal(match.group(1))
            except ValueError:
                continue
            LOGGER.info("whitehills: price via dom = %s", price, extra={"url": url})
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

    def _extract_price_from_json(self, data: Any) -> Any:
        stack = [data]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if isinstance(key, str) and key.lower() in PRICE_JSON_KEYS:
                        if isinstance(value, (str, int, float)):
                            return value
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
        return None


__all__ = ["WhiteHillsParser"]
