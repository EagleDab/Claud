from decimal import Decimal

from scraper.parsers.petrovich import PetrovichParser
from scraper.parsers.whitehills import WhiteHillsParser


def test_whitehills_price_jsonld():
    html = '<script type="application/ld+json">{"@type":"Product","offers":{"price":"1790"}}</script>'
    parser = WhiteHillsParser()
    assert parser.parse_price(html) == Decimal("1790")


def test_whitehills_price_meta():
    html = "<meta itemprop='price' content='1 790,50'>"
    parser = WhiteHillsParser()
    assert parser.parse_price(html) == Decimal("1790.50")


def test_petrovich_price_script():
    html = '<script>{"price":{"current":149}}</script>'
    parser = PetrovichParser()
    assert parser.parse_price(html) == Decimal("149")


def test_whitehills_span_price_value():
    html = '<span class="values_wrapper"><span class="price_value">2 200</span></span>'
    assert WhiteHillsParser().parse_price(html) == Decimal("2200")


def test_petrovich_data_test_price():
    html = '<p data-test="product-retail-price">149<span>\u2009</span><span>₽</span></p>'
    assert PetrovichParser().parse_price(html) == Decimal("149")
