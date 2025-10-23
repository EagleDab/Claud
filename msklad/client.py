"""Client for interacting with the MoySklad REST API."""
from __future__ import annotations

import copy
import logging
import random
import re
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

from pricing.config import settings

LOGGER = logging.getLogger(__name__)

_MAX_RETRIES = 5
_RETRYABLE_STATUSES = {429}


class MoySkladError(RuntimeError):
    """Raised when the MoySklad API returns an error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class MoySkladClient:
    """Lightweight MoySklad API client focusing on price updates."""

    def __init__(
        self,
        base_url: str | None = None,
        token: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        # ``settings.msklad_account_url`` is typed as ``AnyHttpUrl`` in the
        # settings model, which Pydantic represents with its own ``Url`` class.
        # ``Url`` instances do not implement string specific helpers like
        # :meth:`rstrip`, so cast the value to ``str`` before normalising the
        # trailing slash. The cast keeps compatibility with explicit string
        # ``base_url`` arguments and avoids ``AttributeError`` during runtime.
        self.base_url = str(base_url or settings.msklad_account_url).rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json;charset=utf-8",
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
                "Accept-Language": "ru-RU",
            }
        )

        auth_token = token or settings.msklad_token
        if auth_token:
            self.session.headers["Authorization"] = f"Bearer {auth_token}"
        else:
            LOGGER.warning(
                "No MoySklad API token configured; requests will fail with authentication errors",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        url = self._build_url(path)
        attempt = 0
        while True:
            attempt += 1
            LOGGER.debug(
                "MoySklad request",
                extra={"method": method, "url": url, "attempt": attempt, "kwargs": {k: v for k, v in kwargs.items() if k != 'json'}},
            )
            response = self.session.request(method, url, timeout=30, **kwargs)
            if response.status_code >= 400:
                if self._should_retry(response.status_code) and attempt < _MAX_RETRIES:
                    delay = self._retry_delay(attempt)
                    LOGGER.warning(
                        "Retrying MoySklad request",
                        extra={"status": response.status_code, "url": url, "attempt": attempt, "delay": delay},
                    )
                    time.sleep(delay)
                    continue
                self._raise_for_response(response, url)
            break

        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def _should_retry(self, status_code: int) -> bool:
        return status_code in _RETRYABLE_STATUSES or status_code >= 500

    def _retry_delay(self, attempt: int) -> float:
        base = min(2 ** (attempt - 1), 30)
        jitter = random.uniform(0, base / 2)
        return base + jitter

    def _raise_for_response(self, response: requests.Response, url: str) -> None:
        status = response.status_code
        request_id = response.headers.get("X-Lognex-Request-Id") or response.headers.get("X-Request-Id")
        body_text = (response.text or "")[:2000]

        error_code: str | None = None
        error_message: str | None = None
        payload: Dict[str, Any] | None = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    raw_code = first.get("code")
                    if raw_code is not None:
                        error_code = str(raw_code)
                    error_message = first.get("error") or first.get("message")

        log_extra = {
            "status": status,
            "url": url,
            "request_id": request_id,
            "body": body_text,
        }
        if status == 412 and error_code == "1005" and "pricetype" in url:
            LOGGER.error("resource mismatch while creating price type", extra=log_extra)
        else:
            LOGGER.error("MoySklad API error", extra=log_extra)

        message = f"MoySklad API error {status}"
        if error_code:
            message += f" (code {error_code})"
        if request_id:
            message += f", request {request_id}"
        if error_message:
            message += f": {error_message}"
        elif body_text:
            message += f": {body_text}"

        raise MoySkladError(message, status_code=status, code=error_code, request_id=request_id)

    def _generate_external_code(self, name: str, used_codes: set[str]) -> str:
        slug = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_") or "price_type"
        slug = slug[:50]
        candidate = slug
        index = 1
        while candidate in used_codes:
            candidate = f"{slug[:40]}_{index}"
            index += 1
        return candidate

    def _extract_currency_from_settings(
        self, company_settings: Dict[str, Any], price_types: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        for item in price_types:
            currency = item.get("currency")
            if isinstance(currency, dict):
                return copy.deepcopy(currency)
        currency = company_settings.get("currency")
        if isinstance(currency, dict):
            return copy.deepcopy(currency)
        return None

    def _extract_price_type_meta(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        candidate = item.get("priceType")
        if isinstance(candidate, dict):
            meta = candidate.get("meta")
            if isinstance(meta, dict):
                return copy.deepcopy(meta)
        meta = item.get("meta")
        if isinstance(meta, dict):
            return copy.deepcopy(meta)
        return None

    def _sale_price_meta_href(self, sale_price: Dict[str, Any]) -> Optional[str]:
        price_type = sale_price.get("priceType")
        if not isinstance(price_type, dict):
            return None
        meta = price_type.get("meta")
        if not isinstance(meta, dict):
            return None
        href = meta.get("href")
        return href if isinstance(href, str) else None

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------
    def ensure_price_types(self, price_types: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Ensure all requested price types exist and return their metadata mapping."""

        requested: List[str] = []
        for name in price_types:
            if not name:
                continue
            cleaned = name.strip()
            if cleaned and cleaned not in requested:
                requested.append(cleaned)
        if not requested:
            return {}

        LOGGER.info("Ensuring price types: %s", requested)

        company_settings = self._request("GET", "context/companysettings")
        current_price_types: List[Dict[str, Any]] = company_settings.get("priceTypes") or []
        LOGGER.debug(
            "Company settings price types before",
            extra={"price_types": [item.get("name") for item in current_price_types]},
        )

        mapping = {item.get("name"): item for item in current_price_types if item.get("name")}
        missing = [name for name in requested if name not in mapping]

        if missing:
            updated_price_types = list(current_price_types)
            existing_codes = {
                str(item.get("externalCode"))
                for item in current_price_types
                if item.get("externalCode")
            }
            currency_template = self._extract_currency_from_settings(company_settings, current_price_types)
            price_type_template = None
            for item in current_price_types:
                price_type_template = item.get("priceType")
                if price_type_template:
                    break

            for name in missing:
                new_entry: Dict[str, Any] = {"name": name}
                if currency_template:
                    new_entry["currency"] = copy.deepcopy(currency_template)
                if price_type_template:
                    new_entry["priceType"] = copy.deepcopy(price_type_template)
                external_code = self._generate_external_code(name, existing_codes)
                new_entry["externalCode"] = external_code
                existing_codes.add(external_code)
                updated_price_types.append(new_entry)

            payload = {"priceTypes": updated_price_types}
            company_settings = self._request("PUT", "context/companysettings", json=payload)
            current_price_types = company_settings.get("priceTypes") or updated_price_types
        else:
            current_price_types = current_price_types

        LOGGER.debug(
            "Company settings price types after",
            extra={"price_types": [item.get("name") for item in current_price_types]},
        )

        result: Dict[str, Dict[str, Any]] = {}
        for item in current_price_types:
            name = item.get("name")
            if name in requested:
                result[name] = item
        return result

    def get_price_type_mapping(self) -> dict[str, str]:
        """Return mapping of price type names to their meta href."""

        data = self._request("GET", "/context/companysettings")
        types = data.get("priceTypes") or []
        mapping: dict[str, str] = {}
        for item in types:
            if not isinstance(item, dict):
                continue
            try:
                name = item["name"]
                href = item["meta"]["href"]
            except (KeyError, TypeError):
                meta = item.get("priceType")
                try:
                    name = item["name"]
                    href = meta["meta"]["href"]  # type: ignore[index]
                except (KeyError, TypeError):
                    continue
            if isinstance(name, str) and isinstance(href, str):
                mapping[name] = href
        return mapping

    # ------------------------------------------------------------------
    # Product helpers
    # ------------------------------------------------------------------
    def _find_product_meta(self, code: str) -> Optional[dict]:
        params = {"filter": f"code={code}"}
        data = self._request("GET", "entity/product", params=params)
        rows = data.get("rows", [])
        if not rows:
            LOGGER.warning("Product not found in MoySklad", extra={"code": code})
            return None
        return rows[0].get("meta")

    def ensure_min_price(self, product_meta: dict, minimum_value: float = 1.0) -> None:
        """Ensure that the product has minimum price set to avoid MoySklad alerts."""

        product = self._request("GET", product_meta["href"])
        sale_prices: List[dict] = product.get("salePrices", [])
        need_update = False
        for price in sale_prices:
            if price.get("minPrice") is None:
                price["minPrice"] = {"value": int(minimum_value * 100)}
                need_update = True
        if need_update:
            LOGGER.info("Updating product minimum prices", extra={"product": product.get("name")})
            self._request("PUT", product_meta["href"], json={"salePrices": sale_prices})

    def update_product_prices(
        self,
        code: str,
        price_map: Dict[str, float],
        price_types_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        """Update the given price types for a product identified by its code."""

        if not price_map:
            return

        price_types = price_types_meta or self.ensure_price_types(price_map.keys())
        product_meta = self._find_product_meta(code)
        if not product_meta:
            raise MoySkladError(f"Product with code {code} not found")
        product_href = product_meta.get("href")
        if not product_href:
            raise MoySkladError(f"Product with code {code} is missing href metadata")

        # Fetch the latest product state to preserve unrelated sale prices.
        product_data = self._request("GET", product_href)
        existing_sale_prices: Dict[str, Dict[str, Any]] = {}
        for sale_price in product_data.get("salePrices", []) or []:
            href = self._sale_price_meta_href(sale_price)
            if href:
                existing_sale_prices[href] = sale_price
        sale_prices_payload: List[dict] = []
        for price_name, value in price_map.items():
            price_info = price_types.get(price_name)
            if not price_info:
                LOGGER.error(
                    "Price type missing after ensure",
                    extra={"price_type_name": price_name},
                )
                continue
            price_type_meta = self._extract_price_type_meta(price_info)
            if not price_type_meta:
                LOGGER.error(
                    "Price type meta missing",
                    extra={"price_type_name": price_name},
                )
                continue
            meta_href = price_type_meta.get("href") if isinstance(price_type_meta, dict) else None
            existing = existing_sale_prices.get(meta_href) if meta_href else None
            entry = {
                "priceType": {"meta": price_type_meta},
                "value": int(round(value * 100)),
            }
            currency_info = price_info.get("currency")
            if isinstance(currency_info, dict):
                entry["currency"] = copy.deepcopy(currency_info)
            elif existing and isinstance(existing.get("currency"), dict):
                entry["currency"] = copy.deepcopy(existing["currency"])
            if existing and existing.get("minPrice") is not None:
                entry["minPrice"] = copy.deepcopy(existing["minPrice"])
            sale_prices_payload.append(entry)

        if not sale_prices_payload:
            LOGGER.warning("No sale prices to update for %s after filtering payload", code)
            return

        LOGGER.info(
            "Pushing prices to MoySklad",
            extra={"code": code, "price_types": list(price_map.keys()), "count": len(sale_prices_payload)},
        )
        self._request("PUT", product_href, json={"salePrices": sale_prices_payload})
        self.ensure_min_price(product_meta)

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------
    def send_notification(self, message: str) -> None:
        LOGGER.info("MoySklad notification", extra={"message": message})


__all__ = ["MoySkladClient", "MoySkladError"]
