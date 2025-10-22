"""Unit tests for helper functions in the Telegram bot module."""

import pytest

from bot.main import (
    build_product_added_message,
    describe_rule,
    parse_inline_product_payload,
)
from db import PricingRule, RuleType


def test_parse_inline_product_payload_extracts_parts():
    url, code, rules = parse_inline_product_payload(
        "https://example.com/item;ABC123;Цена продажи==; Цена для интернет магазина=10%"
    )

    assert url == "https://example.com/item"
    assert code == "ABC123"
    assert rules == ["Цена продажи==", "Цена для интернет магазина=10%"]


def test_parse_inline_product_payload_requires_url_and_code():
    with pytest.raises(ValueError):
        parse_inline_product_payload("https://example.com/item")


def _make_rule(rule_type: RuleType, value: float, price_type: str) -> PricingRule:
    return PricingRule(rule_type=rule_type, value=value, price_type=price_type)


def test_describe_rule_formats_percent_markup():
    rule = _make_rule(RuleType.PERCENT_MARKUP, 7.5, "Цена продажи")
    assert describe_rule(rule) == "Цена продажи: +7.5%"


def test_describe_rule_formats_minus_fixed():
    rule = _make_rule(RuleType.MINUS_FIXED, 250.0, "Оптовая цена")
    assert describe_rule(rule) == "Оптовая цена: -250"


def test_describe_rule_formats_equal_rule():
    rule = _make_rule(RuleType.EQUAL, 0.0, "Интернет цена")
    assert describe_rule(rule) == "Интернет цена: = цене конкурента"


def test_build_product_added_message_lists_information():
    rules = [_make_rule(RuleType.EQUAL, 0.0, "Цена продажи")]
    message = build_product_added_message(42, ["Цена продажи", "Интернет цена"], rules)

    assert "id=42" in message
    assert "Цена продажи" in message
    assert "Интернет цена" in message
    assert "Активные правила" in message

