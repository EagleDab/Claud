import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest

from msklad.client import MoySkladClient, MoySkladError
from pricing.config import Settings


class DummyResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: Optional[Dict[str, Any]] = None,
        text: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        if text is None and json_data is not None:
            text = json.dumps(json_data)
        self.text = text or ""
        self.content = self.text.encode("utf-8")
        self.headers = headers or {}

    def json(self) -> Dict[str, Any]:
        if self._json_data is None:
            raise ValueError("No JSON payload configured for this response")
        return self._json_data


class DummySession:
    def __init__(self, responses: Iterable[DummyResponse]) -> None:
        self._responses: List[DummyResponse] = list(responses)
        self.requests: List[Dict[str, Any]] = []
        self.headers: Dict[str, str] = {}

    def request(self, method: str, url: str, timeout: Optional[int] = None, **kwargs: Any) -> DummyResponse:
        if not self._responses:
            raise AssertionError("No more responses configured for DummySession")
        self.requests.append({"method": method, "url": url, "kwargs": kwargs})
        return self._responses.pop(0)

    def update_headers(self, values: Dict[str, str]) -> None:
        self.headers.update(values)


@pytest.fixture(autouse=True)
def _silence_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("msklad.client.time.sleep", lambda _: None)
    monkeypatch.setattr("msklad.client.random.uniform", lambda _a, _b: 0.0)


def test_ensure_price_types_creates_missing_via_companysettings() -> None:
    existing = {
        "name": "Retail",
        "externalCode": "retail",
        "priceType": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/context/companysettings/pricetype/retail"}},
        "currency": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/currency/RUB"}},
    }
    created = {
        "name": "Wholesale",
        "externalCode": "Wholesale",
        "priceType": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/context/companysettings/pricetype/wholesale"}},
        "currency": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/currency/RUB"}},
    }
    session = DummySession(
        [
            DummyResponse(json_data={"priceTypes": [existing]}),
            DummyResponse(json_data={"priceTypes": [existing, created]}),
        ]
    )
    client = MoySkladClient(base_url="https://api.moysklad.ru/api/remap/1.2", token="t", session=session)

    mapping = client.ensure_price_types(["Retail", "Wholesale"])

    assert mapping["Wholesale"]["priceType"]["meta"]["href"] == created["priceType"]["meta"]["href"]
    put_payload = session.requests[1]["kwargs"]["json"]["priceTypes"]
    assert any(item["name"] == "Wholesale" for item in put_payload)


def test_update_product_prices_success() -> None:
    price_type_meta = {
        "Retail": {
            "name": "Retail",
            "priceType": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/context/companysettings/pricetype/retail"}},
            "currency": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/currency/RUB"}},
        }
    }
    product_href = "https://api.moysklad.ru/api/remap/1.2/entity/product/123"
    session = DummySession(
        [
            DummyResponse(json_data={"rows": [{"meta": {"href": product_href}}]}),
            DummyResponse(json_data={"salePrices": [{
                "priceType": {"meta": {"href": price_type_meta["Retail"]["priceType"]["meta"]["href"]}},
                "currency": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/currency/RUB"}},
                "minPrice": {"value": 1000},
            }]}),
            DummyResponse(json_data={}),
            DummyResponse(json_data={"salePrices": [{
                "priceType": {"meta": {"href": price_type_meta["Retail"]["priceType"]["meta"]["href"]}},
                "currency": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/currency/RUB"}},
                "minPrice": {"value": 1000},
            }]}),
        ]
    )
    client = MoySkladClient(base_url="https://api.moysklad.ru/api/remap/1.2", token="t", session=session)

    client.update_product_prices("SKU-1", {"Retail": 199.99}, price_type_meta)

    put_request = session.requests[2]
    assert put_request["method"] == "PUT"
    sale_prices = put_request["kwargs"]["json"]["salePrices"]
    assert sale_prices[0]["value"] == int(round(199.99 * 100))
    assert sale_prices[0]["priceType"]["meta"]["href"] == price_type_meta["Retail"]["priceType"]["meta"]["href"]
    assert sale_prices[0]["currency"]["meta"]["href"] == "https://api.moysklad.ru/api/remap/1.2/entity/currency/RUB"


def test_pricetype_wrong_endpoint_regression() -> None:
    existing = {
        "name": "Retail",
        "externalCode": "retail",
        "priceType": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/context/companysettings/pricetype/retail"}},
        "currency": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/currency/RUB"}},
    }
    created = {
        "name": "Wholesale",
        "externalCode": "Wholesale",
        "priceType": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/context/companysettings/pricetype/wholesale"}},
        "currency": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/currency/RUB"}},
    }
    session = DummySession(
        [
            DummyResponse(json_data={"priceTypes": [existing]}),
            DummyResponse(json_data={"priceTypes": [existing, created]}),
        ]
    )
    client = MoySkladClient(base_url="https://api.moysklad.ru/api/remap/1.2", token="t", session=session)

    client.ensure_price_types(["Wholesale"])

    assert all("/entity/pricetype" not in req["url"] for req in session.requests)


def test_retry_on_429_and_5xx_only() -> None:
    base_url = "https://api.moysklad.ru/api/remap/1.2"

    session_429 = DummySession(
        [
            DummyResponse(
                status_code=429,
                json_data={"errors": [{"error": "Too many requests", "code": "429"}]},
                headers={"X-Lognex-Request-Id": "req-429"},
            ),
            DummyResponse(json_data={"ok": True}),
        ]
    )
    client_429 = MoySkladClient(base_url=base_url, token="t", session=session_429)
    assert client_429._request("GET", "entity/product") == {"ok": True}
    assert len(session_429.requests) == 2

    session_500 = DummySession(
        [
            DummyResponse(
                status_code=500,
                json_data={"errors": [{"error": "Server error", "code": "500"}]},
            ),
            DummyResponse(json_data={"ok": True}),
        ]
    )
    client_500 = MoySkladClient(base_url=base_url, token="t", session=session_500)
    assert client_500._request("GET", "entity/product") == {"ok": True}
    assert len(session_500.requests) == 2

    session_412 = DummySession(
        [
            DummyResponse(
                status_code=412,
                json_data={"errors": [{"error": "Unknown type 'pricetype'", "code": 1005}]},
                headers={"X-Lognex-Request-Id": "req-412"},
            )
        ]
    )
    client_412 = MoySkladClient(base_url=base_url, token="t", session=session_412)
    with pytest.raises(MoySkladError) as excinfo:
        client_412._request("POST", "entity/pricetype", json={})
    assert "req-412" in str(excinfo.value)
    assert len(session_412.requests) == 1


def test_config_env_file_typing() -> None:
    settings = Settings()
    env_file = Settings.model_config.get("env_file")
    assert env_file is None or isinstance(env_file, (str, Path))
    assert settings.model_config["env_file_encoding"] == "utf-8"
