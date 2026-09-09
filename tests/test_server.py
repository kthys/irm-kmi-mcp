"""Tests for the MCP tool functions (no MCP runtime needed)."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from irm_kmi_mcp.client import IrmApiClient
from irm_kmi_mcp.server import build_tools
from tests.conftest import fixture_handler


def make_tools(**kwargs):
    transport = httpx.MockTransport(
        fixture_handler(
            search_result=kwargs.pop("search_result", None),
            error=kwargs.pop("error", None),
        )
    )
    client = IrmApiClient(transport=transport, cache_ttl=0)
    return build_tools(client)


def test_current_conditions_tool() -> None:
    tools = make_tools()
    result = tools["current_conditions"]("Namur")
    assert result["city_name"] == "Namur"
    assert result["temperature_c"] == 32
    assert result["condition"] == "clear sky"
    assert result["condition_code"] == 0
    assert result["uv_index"] == 6.3
    assert result["uv_level"] == "High"
    assert result["sunrise"] == "06:25"
    assert "commune" not in result  # caller input is not echoed back


def test_current_conditions_by_coordinates() -> None:
    tools = make_tools()
    result = tools["current_conditions"](latitude=50.4667, longitude=4.8661)
    assert result["city_name"] == "Namur"


def test_location_validation() -> None:
    tools = make_tools()
    with pytest.raises(ValueError, match="Provide either"):
        tools["current_conditions"]()
    with pytest.raises(ValueError, match="latitude must be between"):
        tools["current_conditions"](latitude=91, longitude=4.8)
    with pytest.raises(ValueError, match="longitude must be between"):
        tools["current_conditions"](latitude=50.4, longitude=181)


def test_daily_forecast_default_and_clamping() -> None:
    tools = make_tools()
    assert len(tools["daily_forecast"]("Namur")) == 3
    assert len(tools["daily_forecast"]("Namur", days=9)) == 8
    assert len(tools["daily_forecast"]("Namur", days=0)) == 1


def test_daily_forecast_non_numeric_days_raises_clear_error() -> None:
    tools = make_tools()
    with pytest.raises(ValueError, match="days must be an integer between 1 and 8"):
        tools["daily_forecast"]("Namur", days="abc")


def test_daily_forecast_language() -> None:
    tools = make_tools()
    result = tools["daily_forecast"]("Namur", days=1, language="nl")
    assert result[0]["day_name"] in ("Nacht", "Vannacht", "Donderdag")  # nl variant present
    assert result[0]["text"]


def test_daily_forecast_sun_times_and_text_toggle() -> None:
    tools = make_tools()
    result = tools["daily_forecast"]("Namur", days=2)
    assert result[1]["sunrise"] == "06:27"
    assert result[1]["sunset"] == "21:07"
    assert "text" in result[0]
    compact = tools["daily_forecast"]("Namur", days=2, include_text=False)
    assert all("text" not in day for day in compact)
    assert compact[1]["temp_max_c"] == result[1]["temp_max_c"]


def test_daily_forecast_invalid_language() -> None:
    tools = make_tools()
    with pytest.raises(ValueError, match="Unsupported language"):
        tools["daily_forecast"]("Namur", language="xx")


def test_hourly_forecast_clamping() -> None:
    tools = make_tools()
    assert len(tools["hourly_forecast"]("Namur")) == 24
    assert len(tools["hourly_forecast"]("Namur", hours=100)) == 49
    assert len(tools["hourly_forecast"]("Namur", hours=0)) == 1


def test_hourly_forecast_has_absolute_times(monkeypatch: pytest.MonkeyPatch) -> None:
    from irm_kmi_mcp import models

    fixed_now = datetime(2026, 8, 12, 18, 5, tzinfo=ZoneInfo("Europe/Brussels"))

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(models, "datetime", _FixedDateTime)

    tools = make_tools()
    result = tools["hourly_forecast"]("Namur", hours=49)
    assert all("time" in row for row in result)
    assert all("hour" not in row for row in result)
    assert result[0]["time"] == "2026-08-12T18:00+02:00"
    parsed = [datetime.fromisoformat(row["time"]) for row in result]
    assert all(b - a == timedelta(hours=1) for a, b in itertools.pairwise(parsed))
    assert parsed[6].day != parsed[5].day  # crossed the dateShow boundary


def test_hourly_forecast_non_numeric_hours_raises_clear_error() -> None:
    tools = make_tools()
    with pytest.raises(ValueError, match="hours must be an integer between 1 and 49"):
        tools["hourly_forecast"]("Namur", hours="soon")


def test_warnings_global() -> None:
    tools = make_tools()
    result = tools["warnings"]()
    assert len(result) == 3
    assert result[0]["icon_country"] == "BE"
    assert "level" not in result[0]  # None fields are pruned to save tokens
    assert result[0]["type"] == "heat"


def test_warnings_global_country_filter() -> None:
    tools = make_tools()
    assert len(tools["warnings"](country="BE")) == 1
    assert len(tools["warnings"](country="nl")) == 1  # case-insensitive
    assert tools["warnings"](country="LU")[0]["icon_country"] == "LU"
    with pytest.raises(ValueError, match="Unsupported country"):
        tools["warnings"](country="FR")
    with pytest.raises(ValueError, match="only applies when no location"):
        tools["warnings"]("Namur", country="BE")


def test_warnings_for_commune() -> None:
    tools = make_tools()
    result = tools["warnings"]("Namur")
    assert len(result) == 1
    assert result[0]["type_name"] == "Heat"
    assert result[0]["type"] == "heat"
    assert result[0]["level_label"] == "yellow"
    assert result[0]["from_timestamp"] is not None


def test_rain_forecast_dry() -> None:
    tools = make_tools()
    result = tools["rain_forecast"]("Namur")
    assert result["unit"] == "mm/10min"
    assert result["frames"] == []
    assert result["hint"] == "No rain forecasted shortly"


def test_rain_forecast_invalid_language() -> None:
    tools = make_tools()
    with pytest.raises(ValueError, match="Unsupported language"):
        tools["rain_forecast"]("Namur", language="xx")


def test_pollen_tool() -> None:
    tools = make_tools()
    result = tools["pollen"]()
    assert result["available"] is True
    assert result["levels"] == {
        "grasses": "low",
        "birch": "low",
        "mugwort": "moderate",
        "alder": "very high",
        "hazel": "high",
        "oak": "active",
    }


def test_pollen_uses_english_svg_regardless_of_default_lang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pollen_from_svg only understands English labels, so the SVG must be
    # fetched in English even when IRM_LANG selects another default language.
    import importlib

    import irm_kmi_mcp.constants
    import irm_kmi_mcp.server
    from tests.conftest import load_fixture

    french_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 700 60">'
        '<text><tspan x="100" y="-15"> Bouleau</tspan></text>'
        '<text><tspan x="100" y="33"> faible</tspan></text>'
        "</svg>"
    )
    svg_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "searchCities" in url:
            return httpx.Response(200, json=load_fixture("search_namur.json"))
        if "getSvg" in url:
            svg_calls.append(url)
            text = load_fixture("pollen.svg") if "l=en" in url else french_svg
            return httpx.Response(
                200, text=text, headers={"content-type": "image/svg+xml"}
            )
        return httpx.Response(200, json=load_fixture("forecast_namur.json"))

    monkeypatch.setenv("IRM_LANG", "fr")
    importlib.reload(irm_kmi_mcp.constants)
    importlib.reload(irm_kmi_mcp.server)
    try:
        assert irm_kmi_mcp.server.DEFAULT_LANG == "fr"
        client = IrmApiClient(transport=httpx.MockTransport(handler), cache_ttl=0)
        result = irm_kmi_mcp.server.build_tools(client)["pollen"]()
    finally:
        monkeypatch.delenv("IRM_LANG")
        importlib.reload(irm_kmi_mcp.constants)
        importlib.reload(irm_kmi_mcp.server)

    assert svg_calls and all("l=en" in url for url in svg_calls)
    assert result["available"] is True


def test_pollen_unavailable_without_module() -> None:
    import copy

    import httpx as _httpx

    from tests.conftest import load_fixture

    forecast = copy.deepcopy(load_fixture("forecast_namur.json"))
    forecast["module"] = [m for m in forecast["module"] if m.get("type") != "svg"]

    def handler(request: _httpx.Request) -> _httpx.Response:
        if "searchCities" in str(request.url):
            return _httpx.Response(200, json=load_fixture("search_namur.json"))
        return _httpx.Response(200, json=forecast)

    client = IrmApiClient(transport=_httpx.MockTransport(handler), cache_ttl=0)
    tools = build_tools(client)
    assert tools["pollen"]() == {"available": False}


def test_unknown_commune_raises_helpful_error() -> None:
    tools = make_tools(search_result=[])
    with pytest.raises(ValueError, match="Municipality not found"):
        tools["current_conditions"]("Atlantide")


def test_api_error_propagates(client_factory) -> None:
    tools = build_tools(client_factory(error=__import__("httpx").ConnectError("boom")))
    with pytest.raises(Exception, match="IRM API request failed"):
        tools["current_conditions"]("Namur")


def test_env_default_lang(monkeypatch) -> None:
    from irm_kmi_mcp import constants

    monkeypatch.delenv("IRM_LANG", raising=False)
    assert constants._env_default_lang() == "en"

    monkeypatch.setenv("IRM_LANG", "nl")
    assert constants._env_default_lang() == "nl"

    monkeypatch.setenv("IRM_LANG", "DE")  # case-insensitive
    assert constants._env_default_lang() == "de"

    monkeypatch.setenv("IRM_LANG", "xx")  # unsupported → fallback, warns
    assert constants._env_default_lang() == "en"

    assert constants.DEFAULT_LANG in constants.SUPPORTED_LANGS
