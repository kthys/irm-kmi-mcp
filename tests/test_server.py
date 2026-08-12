"""Tests for the MCP tool functions (no MCP runtime needed)."""

from __future__ import annotations

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


def test_daily_forecast_invalid_language() -> None:
    tools = make_tools()
    with pytest.raises(ValueError, match="Unsupported language"):
        tools["daily_forecast"]("Namur", language="xx")


def test_hourly_forecast_clamping() -> None:
    tools = make_tools()
    assert len(tools["hourly_forecast"]("Namur")) == 24
    assert len(tools["hourly_forecast"]("Namur", hours=100)) == 49
    assert len(tools["hourly_forecast"]("Namur", hours=0)) == 1


def test_hourly_forecast_non_numeric_hours_raises_clear_error() -> None:
    tools = make_tools()
    with pytest.raises(ValueError, match="hours must be an integer between 1 and 49"):
        tools["hourly_forecast"]("Namur", hours="soon")


def test_warnings_global() -> None:
    tools = make_tools()
    result = tools["warnings"]()
    assert len(result) == 3
    assert result[0]["icon_country"] == "BE"
    assert result[0]["level"] is None


def test_warnings_for_commune() -> None:
    tools = make_tools()
    result = tools["warnings"]("Namur")
    assert len(result) == 1
    assert result[0]["type_name"] == "Heat"
    assert result[0]["level_label"] == "yellow"
    assert result[0]["from_timestamp"] is not None


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
