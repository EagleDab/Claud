"""Tests for the MoySklad client configuration."""

from msklad.client import MoySkladClient


def test_client_sets_required_accept_header() -> None:
    """The MoySklad API requires the Accept header to include utf-8 charset."""

    client = MoySkladClient(base_url="https://example.com", token="test-token")

    assert client.session.headers["Accept"] == "application/json;charset=utf-8"
