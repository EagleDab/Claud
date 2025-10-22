import pytest

from scraper.parsers.petrovich import PetrovichParser
from scraper.parsers.whitehills import WhiteHillsParser
from scraper.parsers.mk4s import MK4SParser


@pytest.mark.asyncio
async def test_petrovich_parser_extracts_price(monkeypatch):
    parser = PetrovichParser()
    html = """
    <html><body>
        <h1>Product Name</h1>
        <span class="product-price__current">1 234 ₽</span>
    </body></html>
    """

    async def fake_fetch(url):
        return html

    monkeypatch.setattr(parser, "fetch_html", fake_fetch)
    result = await parser.fetch_product("https://moscow.petrovich.ru/p/sku-1")
    assert result.price == 1234.0
    assert result.currency == "RUB"


@pytest.mark.asyncio
async def test_whitehills_parser(monkeypatch):
    parser = WhiteHillsParser()
    html = """
    <html><body>
        <div class="product-card__price-current"><span>2 500 ₽</span></div>
    </body></html>
    """

    async def fake_fetch(url):
        return html

    monkeypatch.setattr(parser, "fetch_html", fake_fetch)
    result = await parser.fetch_product("https://whitehills.ru/p/sku-1")
    assert result.price == 2500.0


@pytest.mark.asyncio
async def test_mk4s_parser_variants(monkeypatch):
    parser = MK4SParser()
    html = """
    <html><body>
      <script type="application/json">
        {"product": {"name": "Variant product", "variants": {"Blue": {"price": 3333, "sku": "BLU"}}}}
      </script>
    </body></html>
    """

    async def fake_fetch(url):
        return html

    monkeypatch.setattr(parser, "fetch_html", fake_fetch)
    result = await parser.fetch_product("https://mk4s.ru/p/sku-1", variant="Blue")
    assert result.price == 3333
    assert result.sku == "BLU"
