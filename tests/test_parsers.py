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
