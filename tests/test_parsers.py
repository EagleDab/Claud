import pytest
from decimal import Decimal
from unittest.mock import AsyncMock

from scraper.parsers.whitehills import WhiteHillsParser
from scraper.parsers.petrovich import PetrovichParser


@pytest.mark.asyncio
async def test_whitehills_price_from_jsonld(monkeypatch):
    parser = WhiteHillsParser()
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "Stone",
          "offers": {"price": "1234"}
        }
        </script>
      </head>
      <body><h1>Stone</h1></body>
    </html>
    """
    monkeypatch.setattr(parser, "fetch_html", AsyncMock(return_value=html))

    snapshot = await parser.fetch_product("https://whitehills.ru/product/test/")

    assert snapshot.price == Decimal("1234.00")


@pytest.mark.asyncio
async def test_whitehills_price_from_fallback(monkeypatch):
    parser = WhiteHillsParser()
    html = """
    <html>
      <head>
        <meta itemprop="price" content="2 345,50" />
      </head>
      <body><h1>Fallback</h1></body>
    </html>
    """
    monkeypatch.setattr(parser, "fetch_html", AsyncMock(return_value=html))

    snapshot = await parser.fetch_product("https://whitehills.ru/product/test/")

    assert snapshot.price == Decimal("2345.50")


@pytest.mark.asyncio
async def test_petrovich_price_from_jsonld_or_script(monkeypatch):
    parser = PetrovichParser()
    html_jsonld = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "Tile",
          "offers": {"price": "1111"}
        }
        </script>
      </head>
      <body><h1>Tile</h1></body>
    </html>
    """
    monkeypatch.setattr(parser, "fetch_html", AsyncMock(return_value=html_jsonld))

    snapshot = await parser.fetch_product("https://moscow.petrovich.ru/product/126426/")

    assert snapshot.price == Decimal("1111.00")

    html_script = """
    <html>
      <head>
        <script>
          window.__INITIAL_STATE__ = {"currentPrice": 999.99};
        </script>
      </head>
      <body><h1>Tile</h1></body>
    </html>
    """
    monkeypatch.setattr(parser, "fetch_html", AsyncMock(return_value=html_script))

    snapshot = await parser.fetch_product("https://moscow.petrovich.ru/product/126426/")

    assert snapshot.price == Decimal("999.99")
