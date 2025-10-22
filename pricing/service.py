"""Pricing orchestration and monitoring logic."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Iterable, List, Optional, Dict, Any

from sqlalchemy.orm import Session

from db import PriceEvent, PricingRule, Product, session_scope
from db.models import CategoryItem
from msklad import MoySkladClient
from pricing.config import settings
from pricing.rules import apply_pricing_rules, merge_rules
from scraper import ScraperService


class PriceMonitorService:
    """Service responsible for checking competitor products and syncing prices."""

    def __init__(
        self,
        session: Session,
        scraper: Optional[ScraperService] = None,
        msklad_client: Optional[MoySkladClient] = None,
    ) -> None:
        self.session = session
        self.scraper = scraper or ScraperService()
        self.msklad_client = msklad_client or MoySkladClient()

    # ------------------------------------------------------------------
    async def check_product(self, product: Product) -> Optional[PriceEvent]:
        """Fetch competitor price and update MoySklad if required."""

        if not product.enabled:
            return None
        adapter_name = product.site.parser_adapter
        snapshot = await self.scraper.fetch_product(adapter_name, product.competitor_url, variant=product.variant_key)
        new_price = snapshot.price
        last_price = float(product.last_price) if product.last_price is not None else None

        price_changed = last_price is None or abs(last_price - new_price) > 0.0001
        product.last_price = new_price
        product.last_checked_at = datetime.utcnow()
        if not price_changed:
            return None

        event = PriceEvent(
            product=product,
            old_price=last_price,
            new_price=new_price,
            detected_at=datetime.utcnow(),
            payload={"snapshot": snapshot.__dict__},
        )
        self.session.add(event)

        price_map = self._build_price_map(product, new_price)
        if price_map:
            await self._push_to_msklad(product, price_map)
            event.pushed_to_msklad = True
        return event

    # ------------------------------------------------------------------
    def _build_price_map(self, product: Product, price: float) -> dict[str, float]:
        rules = merge_rules(product.pricing_rules, self._category_rules(product))
        if not rules:
            fallback_types: List[str] = []
            for link in product.links:
                fallback_types.extend(link.price_types)
            if not fallback_types:
                fallback_types = settings.default_price_types
            mapping = apply_pricing_rules(price, [], fallback_price_types=fallback_types)
            return dict(mapping.items())

        mapping = apply_pricing_rules(price, rules)
        return dict(mapping.items())

    def _category_rules(self, product: Product) -> Iterable[PricingRule]:
        category_ids = [row.category_id for row in self.session.query(CategoryItem).filter_by(product_id=product.id)]
        if not category_ids:
            return []
        return self.session.query(PricingRule).filter(PricingRule.category_id.in_(category_ids)).all()

    async def _push_to_msklad(self, product: Product, price_map: dict[str, float]) -> None:
        tasks = []
        for link in product.links:
            if not link.auto_update:
                continue
            payload = {price_type: price_map.get(price_type, price_map.get(settings.default_price_types[0])) for price_type in link.price_types}
            tasks.append(asyncio.to_thread(self.msklad_client.update_product_prices, link.msklad_code, payload))
        if tasks:
            await asyncio.gather(*tasks)


async def check_all_products(batch_size: int | None = None) -> List[Dict[str, Any]]:
    """Process products in batches and return detected events as dictionaries."""

    events: List[Dict[str, Any]] = []
    batch = batch_size or settings.price_check_batch_size
    with session_scope() as session:
        products = (
            session.query(Product)
            .filter_by(enabled=True)
            .order_by(Product.last_checked_at.nullsfirst())
            .limit(batch)
            .all()
        )
        service = PriceMonitorService(session)
        for product in products:
            event = await service.check_product(product)
            if event:
                session.flush()
                events.append(
                    {
                        "product_id": product.id,
                        "competitor_url": product.competitor_url,
                        "product_title": product.title,
                        "old_price": float(event.old_price) if event.old_price is not None else None,
                        "new_price": float(event.new_price),
                        "msklad_codes": [link.msklad_code for link in product.links],
                        "price_types": [price_type for link in product.links for price_type in link.price_types],
                    }
                )
        session.flush()
    return events


__all__ = ["PriceMonitorService", "check_all_products"]
