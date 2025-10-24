from decimal import Decimal

from scraper.parsers.whitehills import WhiteHillsParser


def test_jsonld_price():
    html = '<script type="application/ld+json">{"@type":"Product","offers":{"price":"1790"}}</script>'
    assert WhiteHillsParser().parse_price(html) == Decimal("1790")


def test_dom_price_value():
    html = '<span class="values_wrapper"><span class="price_value">2\u00A0200</span></span>'
    assert WhiteHillsParser().parse_price(html) == Decimal("2200")
