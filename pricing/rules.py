"""Pricing rules engine used for updating MoySklad prices."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, List, Mapping, MutableMapping, Optional

from db.models import PricingRule, RuleType


@dataclass(slots=True)
class PricingRuleSpec:
    """In-memory representation of a pricing rule."""

    rule_type: RuleType
    value: float
    price_type: str
    priority: int = 10

    @classmethod
    def from_model(cls, model: PricingRule) -> "PricingRuleSpec":
        return cls(
            rule_type=model.rule_type,
            value=model.value,
            price_type=model.price_type,
            priority=model.priority,
        )


def apply_rule(price: float, spec: PricingRuleSpec) -> float:
    """Apply a single rule to the competitor price."""

    if spec.rule_type == RuleType.PERCENT_MARKUP:
        return price * (1 + spec.value / 100.0)
    if spec.rule_type == RuleType.MINUS_FIXED:
        return max(price - spec.value, 0)
    if spec.rule_type == RuleType.EQUAL:
        return price
    raise ValueError(f"Unsupported rule type {spec.rule_type}")


def round_price(value: float) -> float:
    """Round the price to two decimal places using bankers rounding."""

    decimal_value = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(decimal_value)


def apply_pricing_rules(
    competitor_price: float,
    rules: Iterable[PricingRuleSpec],
    *,
    fallback_price_types: Optional[Iterable[str]] = None,
) -> Mapping[str, float]:
    """Apply provided rules and return mapping price_type->value."""

    rule_list = sorted(rules, key=lambda r: (r.priority, r.price_type))
    result: MutableMapping[str, float] = {}
    for rule in rule_list:
        updated_price = round_price(apply_rule(competitor_price, rule))
        result[rule.price_type] = updated_price

    if not result and fallback_price_types:
        rounded = round_price(competitor_price)
        for price_type in fallback_price_types:
            result[price_type] = rounded

    return result


def merge_rules(*rule_groups: Iterable[PricingRule]) -> List[PricingRuleSpec]:
    """Merge ORM rule objects into a deduplicated list."""

    specs: List[PricingRuleSpec] = []
    for group in rule_groups:
        for rule in group:
            specs.append(PricingRuleSpec.from_model(rule))
    return specs


__all__ = [
    "PricingRuleSpec",
    "apply_pricing_rules",
    "merge_rules",
]
