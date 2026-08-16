"""Tests for the IRM API client: key derivation, parsing, caching, errors."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from irm_kmi_mcp.client import City, IrmApiClient, IrmApiError
from irm_kmi_mcp.constants import APP_SECRET
from tests.conftest import fixture_handler

# Reference value computed for a fixed date (formula: md5(secret;service;DD/MM/YYYY)).
REFERENCE_KEY = "c26bffd382365c869a3d168d92342165"


def test_api_key_format() -> None:
    key = IrmApiClient._api_key("getForecasts", when=datetime(2024, 12, 31, tzinfo=timezone.utc))
    assert key == REFERENCE_KEY
    assert len(key) == 32  # md5 hex digest


def test_api_key_changes_daily() -> None:
    key1 = IrmApiClient._api_key("getForecasts", when=datetime(2024, 12, 31, tzinfo=timezone.utc))
    key2 = IrmApiClient._api_key("getForecasts", when=datetime(2025, 1, 1, tzinfo=timezone.utc))
    assert key1 != key2


def test_search_cities_parses_response(client: IrmApiClient) -> None:
    cities = client.search_cities("Namur")
    assert cities == [City(id="92094", name="Namur (BE)")]


def test_search_cities_empty(client_factory) -> None:
    client = client_factory(search_result=[])
    assert client.search_cities("Atlantide") == []


def test_search_cities_skips_items_missing_name(client_factory) -> None:
    # The upstream sometimes returns partial entries. An item with `id` but no
    # `name` must be skipped rather than raise an uncaught KeyError.
    client = client_factory(
        search_result=[
            {"id": "123"},
            {"id": "92094", "name": "Namur (BE)"},
            {"name": "No ID"},
            "not a dict",
        ]
    )
    assert client.search_cities("Namur") == [City(id="92094", name="Namur (BE)")]


def test_get_forecasts_returns_raw(client: IrmApiClient) -> None:
    raw = client.get_forecasts("92094", lang="fr")
    assert raw["cityName"] == "Namur"
    assert "obs" in raw and "for" in raw


def test_get_forecasts_coord_sends_coordinates() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return fixture_handler()(request)

    client = IrmApiClient(transport=httpx.MockTransport(handler), cache_ttl=0)
    raw = client.get_forecasts_coord(50.4667004, 4.8661387)
    assert raw["cityName"] == "Namur"
    assert "lat=50.4667" in calls[0]
    assert "long=4.866139" in calls[0]
    assert "ins=" not in calls[0]


def test_get_svg_returns_text_and_caches() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return fixture_handler()(request)

    client = IrmApiClient(transport=httpx.MockTransport(handler), cache_ttl=60)
    url = "https://app.meteo.be/services/appv4/?s=getSvg&e=pollen&l=en&k=abc"
    svg = client.get_svg(url)
    assert svg.startswith("<?xml")
    assert "<svg" in svg
    assert client.get_svg(url) == svg
    assert len(calls) == 1


def test_get_svg_error_is_wrapped(client_factory) -> None:
    client = client_factory(error=httpx.ConnectError("boom"))
    with pytest.raises(IrmApiError, match="IRM SVG request failed"):
        client.get_svg("https://app.meteo.be/services/appv4/?s=getSvg&e=pollen")


def test_transient_errors_are_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("transient")
        return fixture_handler()(request)

    client = IrmApiClient(transport=httpx.MockTransport(handler), cache_ttl=0)
    raw = client.get_forecasts("92094")
    assert raw["cityName"] == "Namur"
    assert attempts["n"] == 3


def test_http_status_errors_are_not_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, text="boom")

    client = IrmApiClient(transport=httpx.MockTransport(handler), cache_ttl=0)
    with pytest.raises(IrmApiError, match="IRM API request failed"):
        client.get_forecasts("92094")
    assert attempts["n"] == 1


def test_get_global_warnings(client: IrmApiClient) -> None:
    warnings = client.get_global_warnings("fr")
    assert len(warnings) == 3
    assert all("warningType" in w for w in warnings)


def test_cache_avoids_second_request() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return fixture_handler()(request)

    client = IrmApiClient(transport=httpx.MockTransport(handler), cache_ttl=60)
    client.get_forecasts("92094")
    client.get_forecasts("92094")
    assert len(calls) == 1


def test_cache_expires() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return fixture_handler()(request)

    client = IrmApiClient(transport=httpx.MockTransport(handler), cache_ttl=0)
    client.get_forecasts("92094")
    client.get_forecasts("92094")
    assert len(calls) == 2


def test_city_cache_avoids_repeat_lookup() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return fixture_handler()(request)

    # data cache disabled so only the city lookup cache can serve the repeat call
    client = IrmApiClient(transport=httpx.MockTransport(handler), cache_ttl=0, city_cache_ttl=60)
    client.search_cities("Namur")
    client.search_cities("Namur")
    assert len(calls) == 1


def test_http_error_is_wrapped(client_factory) -> None:
    client = client_factory(error=httpx.ConnectError("boom"))
    with pytest.raises(IrmApiError, match="IRM API request failed"):
        client.get_forecasts("92094")


def test_expired_cache_entries_are_evicted() -> None:
    client = IrmApiClient(
        transport=httpx.MockTransport(fixture_handler()), cache_ttl=0, city_cache_ttl=0
    )
    from irm_kmi_mcp import constants

    # Simulate a cache full of expired entries.
    for i in range(constants._CACHE_EVICTION_THRESHOLD + 10):
        client._data_cache[("svc", str(i))] = (0.0, {})
    client.get_forecasts("92094")
    # Expired entries were dropped; only the fresh one remains.
    assert len(client._data_cache) == 1


def test_api_key_normalized_to_brussels_timezone() -> None:
    # 2024-12-31 23:30 UTC == 2025-01-01 00:30 Brussels (next calendar day).
    # A host whose clock is behind Brussels (the Americas, or even UTC near
    # midnight) would otherwise derive the previous day's key and fail every
    # request during the daily rollover window.
    when = datetime(2024, 12, 31, 23, 30, tzinfo=timezone.utc)
    key = IrmApiClient._api_key("getForecasts", when=when)
    expected = hashlib.md5(f"{APP_SECRET};getForecasts;01/01/2025".encode()).hexdigest()
    assert key == expected


def test_api_key_default_path_uses_brussels(monkeypatch) -> None:
    # The no-arg path must call datetime.now() with Europe/Brussels, not rely
    # on the host's local timezone (datetime.now().astimezone()).
    import irm_kmi_mcp.client as client_mod

    captured: list[object] = []
    fake_brussels = datetime(2025, 1, 1, 0, 30, tzinfo=ZoneInfo("Europe/Brussels"))

    class FakeDateTime:
        @staticmethod
        def now(tz=None):
            captured.append(tz)
            return fake_brussels

    monkeypatch.setattr(client_mod, "datetime", FakeDateTime)
    IrmApiClient._api_key("getForecasts")
    assert captured == [ZoneInfo("Europe/Brussels")]
