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


@pytest.mark.asyncio
async def test_mk4s_parser_handles_json_assignment(monkeypatch):
    parser = MK4SParser()
    html = """
    <html><body>
      <script>
        window.__NUXT__ = {"product": {"name": "Assigned JSON", "variants": {"Green": {"price": 4444, "sku": "GRN"}}}};
      </script>
    </body></html>
    """

    async def fake_fetch(url):
        return html

    monkeypatch.setattr(parser, "fetch_html", fake_fetch)
    result = await parser.fetch_product("https://mk4s.ru/p/sku-2", variant="Green")
    assert result.price == 4444
    assert result.sku == "GRN"


@pytest.mark.asyncio
async def test_mk4s_parser_fallbacks_to_dom(monkeypatch):
    parser = MK4SParser()
    html = """
    <html><body>
      <div class="product-add-to-cart__price">1 999 ₽</div>
      <div class="block block_secondary">
        <div class="block__header">Толщина</div>
        <div class="product-feature-select__value">0.45 мм</div>
        <div class="product-feature-select__value">0.50 мм</div>
      </div>
      <div class="block block_secondary">
        <div class="block__header">Цвет</div>
        <div class="product-feature-select__color-wrapper">
          <span class="tooltip__content">Красный</span>
          <span class="tooltip__content">Серый</span>
        </div>
      </div>
    </body></html>
    """

    async def fake_fetch(url):
        return html

    monkeypatch.setattr(parser, "fetch_html", fake_fetch)
    result = await parser.fetch_product("https://mk4s.ru/p/sku-3", variant="0.50 мм|Серый")

    assert result.price == 1999.0
    assert result.variant_key == "0.50 мм|Серый"
    assert result.payload == {"variant": {"Толщина": "0.50 мм", "Цвет": "Серый"}}
