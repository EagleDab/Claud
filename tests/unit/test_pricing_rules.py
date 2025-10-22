from pricing.rules import PricingRuleSpec, apply_pricing_rules
from db.models import RuleType


def test_percent_markup_rule():
    rule = PricingRuleSpec(rule_type=RuleType.PERCENT_MARKUP, value=20, price_type="Цена продажи")
    result = apply_pricing_rules(100, [rule])
    assert result["Цена продажи"] == 120.0


def test_minus_fixed_rule():
    rule = PricingRuleSpec(rule_type=RuleType.MINUS_FIXED, value=5, price_type="Цена для интернет-магазина")
    result = apply_pricing_rules(100, [rule])
    assert result["Цена для интернет-магазина"] == 95.0


def test_equal_rule_with_multiple_price_types():
    rules = [
        PricingRuleSpec(rule_type=RuleType.EQUAL, value=0, price_type="Розница"),
        PricingRuleSpec(rule_type=RuleType.MINUS_FIXED, value=1, price_type="Интернет"),
    ]
    result = apply_pricing_rules(99.99, rules)
    assert result["Розница"] == 99.99
    assert result["Интернет"] == 98.99


def test_fallback_price_types():
    result = apply_pricing_rules(50, [], fallback_price_types=["Розница", "Интернет"])
    assert result["Розница"] == 50.0
    assert result["Интернет"] == 50.0
