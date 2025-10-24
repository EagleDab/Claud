from decimal import Decimal

from scraper.parsers.whitehills import to_decimal


def test_whitehills_spaces():
    assert to_decimal("2\u00A0\u202F200 ₽") == Decimal("2200")
