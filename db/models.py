"""Database models describing monitored sites, products and pricing rules."""
from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


class RuleType(str, enum.Enum):
    """Pricing rule types."""

    PERCENT_MARKUP = "PERCENT_MARKUP"
    MINUS_FIXED = "MINUS_FIXED"
    EQUAL = "EQUAL"


class Site(Base):
    """Represents a competitor site."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_url: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parser_adapter: Mapped[str] = mapped_column(String(120), nullable=False)
    rate_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    products: Mapped[List["Product"]] = relationship("Product", back_populates="site", cascade="all, delete-orphan")
    categories: Mapped[List["Category"]] = relationship("Category", back_populates="site", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_sites_enabled", "enabled"),
    )


class Category(Base):
    """A category page that contains multiple products."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    category_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    site: Mapped[Site] = relationship("Site", back_populates="categories")
    items: Mapped[List["CategoryItem"]] = relationship(
        "CategoryItem",
        back_populates="category",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("site_id", "category_url", name="uq_category_site_url"),
    )


class Product(Base):
    """Represents a competitor product tied to a MoySklad product."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    competitor_url: Mapped[str] = mapped_column(Text, nullable=False)
    competitor_sku: Mapped[Optional[str]] = mapped_column(String(120))
    title: Mapped[Optional[str]] = mapped_column(String(255))
    variant_key: Mapped[Optional[str]] = mapped_column(String(255))
    last_price: Mapped[Optional[float]] = mapped_column(Numeric(precision=12, scale=2))
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    poll_interval_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    site: Mapped[Site] = relationship("Site", back_populates="products")
    links: Mapped[List["MSkladLink"]] = relationship("MSkladLink", back_populates="product", cascade="all, delete-orphan")
    pricing_rules: Mapped[List["PricingRule"]] = relationship(
        "PricingRule",
        back_populates="product",
        cascade="all, delete-orphan",
    )
    price_events: Mapped[List["PriceEvent"]] = relationship(
        "PriceEvent",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="PriceEvent.detected_at",
    )

    __table_args__ = (
        Index("ix_product_enabled", "enabled"),
        UniqueConstraint("site_id", "competitor_url", "variant_key", name="uq_product_variant"),
    )


class CategoryItem(Base):
    """Link table between categories and products."""

    __tablename__ = "category_items"

    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)

    category: Mapped[Category] = relationship("Category", back_populates="items")
    product: Mapped[Product] = relationship("Product")


class MSkladLink(Base):
    """Mapping between competitor product and MoySklad codes/price types."""

    __tablename__ = "msklad_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    msklad_code: Mapped[str] = mapped_column(String(120), nullable=False)
    price_types: Mapped[List[str]] = mapped_column(ARRAY(String(120)), nullable=False, default=list)
    auto_update: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    product: Mapped[Product] = relationship("Product", back_populates="links")

    __table_args__ = (
        UniqueConstraint("product_id", "msklad_code", name="uq_msklad_link_product_code"),
    )


class PricingRule(Base):
    """Stores pricing rules per product or category."""

    __tablename__ = "pricing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=True)
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), nullable=True)
    rule_type: Mapped[RuleType] = mapped_column(Enum(RuleType), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    price_type: Mapped[str] = mapped_column(String(120), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

    product: Mapped[Optional[Product]] = relationship("Product", back_populates="pricing_rules")
    category: Mapped[Optional[Category]] = relationship("Category")

    __table_args__ = (
        CheckConstraint(
            "(product_id IS NOT NULL)::integer + (category_id IS NOT NULL)::integer = 1",
            name="ck_rule_scope",
        ),
        Index("ix_pricing_rules_price_type", "price_type"),
    )


class PriceEvent(Base):
    """History of price changes detected for a product."""

    __tablename__ = "price_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    old_price: Mapped[Optional[float]] = mapped_column(Numeric(precision=12, scale=2), nullable=True)
    new_price: Mapped[float] = mapped_column(Numeric(precision=12, scale=2), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    pushed_to_msklad: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    product: Mapped[Product] = relationship("Product", back_populates="price_events")

    __table_args__ = (
        Index("ix_price_events_detected_at", "detected_at"),
    )


__all__ = [
    "Base",
    "Site",
    "Product",
    "Category",
    "CategoryItem",
    "MSkladLink",
    "PricingRule",
    "PriceEvent",
    "RuleType",
]
