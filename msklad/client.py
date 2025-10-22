"""Client for interacting with the MoySklad REST API."""
from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional

import requests

from pricing.config import settings

LOGGER = logging.getLogger(__name__)


class MoySkladError(RuntimeError):
    """Raised when the MoySklad API returns an error."""


class MoySkladClient:
    """Lightweight MoySklad API client focusing on price updates."""

    def __init__(
        self,
        base_url: str | None = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
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
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        elif username and password:
            self.session.auth = (username, password)
        elif settings.msklad_token:
            self.session.headers["Authorization"] = f"Bearer {settings.msklad_token}"
        elif settings.msklad_username and settings.msklad_password:
            self.session.auth = (settings.msklad_username, settings.msklad_password)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}/{path.lstrip('/') }"
        LOGGER.debug("MoySklad request", extra={"method": method, "url": url, "kwargs": kwargs})
        response = self.session.request(method, url, timeout=30, **kwargs)
        if response.status_code >= 400:
            LOGGER.error(
                "MoySklad API error", extra={"status": response.status_code, "body": response.text[:500]}
            )
            raise MoySkladError(f"MoySklad API error {response.status_code}: {response.text}")
        if not response.text:
            return {}
        return response.json()

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------
    def get_price_type_mapping(self) -> Dict[str, str]:
        """Return mapping of price type names to their href values."""

        data = self._request("GET", "/entity/product/metadata")
        price_types = data.get("priceTypes", [])
        mapping = {item["name"].strip(): item["meta"]["href"] for item in price_types}
        LOGGER.debug("Loaded price types", extra={"count": len(mapping)})
        return mapping

    def ensure_price_types(self, price_types: Iterable[str]) -> Dict[str, str]:
        """Ensure all requested price types exist and return their metadata mapping."""

        existing = self.get_price_type_mapping()
        missing = [name for name in price_types if name not in existing]
        for name in missing:
            payload = {"name": name}
            LOGGER.info("Creating missing price type", extra={"name": name})
            data = self._request("POST", "/entity/pricetype", json=payload)
            meta = data.get("meta", {}).get("href")
            if meta:
                existing[name] = meta
        return {name: existing[name] for name in price_types if name in existing}

    # ------------------------------------------------------------------
    # Product helpers
    # ------------------------------------------------------------------
    def _find_product_meta(self, code: str) -> Optional[dict]:
        params = {"filter": f"code={code}"}
        data = self._request("GET", "/entity/product", params=params)
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

    def update_product_prices(self, code: str, price_map: Dict[str, float]) -> None:
        """Update the given price types for a product identified by its code."""

        if not price_map:
            return
        price_types = self.ensure_price_types(price_map.keys())
        product_meta = self._find_product_meta(code)
        if not product_meta:
            raise MoySkladError(f"Product with code {code} not found")

        sale_prices_payload = []
        for price_name, value in price_map.items():
            meta_href = price_types.get(price_name)
            if not meta_href:
                LOGGER.error("Price type missing after ensure", extra={"name": price_name})
                continue
            sale_prices_payload.append({
                "priceType": {"meta": {"href": meta_href}},
                "value": int(round(value * 100)),
            })

        LOGGER.info(
            "Pushing prices to MoySklad",
            extra={"code": code, "price_types": list(price_map.keys()), "count": len(sale_prices_payload)},
        )
        self._request("PUT", product_meta["href"], json={"salePrices": sale_prices_payload})
        self.ensure_min_price(product_meta)

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------
    def send_notification(self, message: str) -> None:
        LOGGER.info("MoySklad notification", extra={"message": message})


__all__ = ["MoySkladClient", "MoySkladError"]
