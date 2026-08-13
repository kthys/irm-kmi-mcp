"""Tests for the model parsing/normalisation, backed by recorded fixtures."""

from __future__ import annotations

from irm_kmi_mcp.models import (
    CurrentConditions,
    DailyForecast,
    HourlyForecast,
    WeatherWarning,
)
from tests.conftest import load_fixture


def test_current_conditions_from_raw() -> None:
    raw = load_fixture("forecast_namur.json")
    uv = {"levelValue": 6.3, "level": {"en": "High", "fr": "Élevé"}}
    sun = {"sunrise": "23100", "sunset": "76140"}
    c = CurrentConditions.from_raw("Namur", raw, uv, sun)
    assert c.city_name == "Namur"
    assert c.temperature_c == 32
    assert c.condition == "clear sky"
    assert c.condition_code == 0
    assert c.day_night == "d"
    assert c.uv_index == 6.3
    assert c.uv_level == "High"
    assert c.sunrise == "06:25"
    assert c.sunset == "21:09"


def test_current_conditions_tolerates_missing_data() -> None:
    c = CurrentConditions.from_raw("X", {"cityName": "X", "obs": {}})
    assert c.temperature_c is None
    assert c.condition is None
    assert c.condition_code is None


def test_daily_forecast_parsing() -> None:
    raw = load_fixture("forecast_namur.json")
    items = raw["for"]["daily"]
    day = DailyForecast.from_raw(items[1], "fr")
    assert day.day_name == "Jeudi"
    assert day.temp_max_c is not None
    assert day.condition_day == "clear sky"
    assert day.condition_day_code == 0
    assert day.text  # official bulletin, French
    assert day.wind_direction in ("VAR", "SE", "NE") or day.wind_direction is not None


def test_daily_forecast_english_text() -> None:
    raw = load_fixture("forecast_namur.json")
    day = DailyForecast.from_raw(raw["for"]["daily"][1], "en")
    assert day.day_name == "Thursday"
    assert day.text  # English bulletin


def test_hourly_forecast_parsing() -> None:
    raw = load_fixture("forecast_namur.json")
    hour = HourlyForecast.from_raw(raw["for"]["hourly"][0], "fr")
    assert hour.hour == "18"
    assert hour.temperature_c == 32
    assert hour.condition == "clear sky"  # ww arrives as a string here
    assert hour.condition_code == 0
    assert hour.pressure_hpa == 1022
    assert hour.wind_kmh == 10


def test_hourly_wind_direction_normalized() -> None:
    """IRM degrees point *toward* the wind; the model must expose the bearing."""
    raw = load_fixture("forecast_namur.json")
    hour = HourlyForecast.from_raw(raw["for"]["hourly"][0], "fr")
    # Fixture: windDirection=225, windDirectionText={"fr": "NE", ...}.
    # 225° toward NE == wind coming from 45° (NE): degrees must match the text.
    assert hour.wind_direction == 45.0
    assert hour.wind_direction_text == "NE"


def test_hourly_wind_direction_variable() -> None:
    """Variable wind (VAR/VER) yields no bearing, whatever the language."""
    item = {
        "hour": "12",
        "windDirection": 0,
        "windDirectionText": {"fr": "VAR", "nl": "VER", "en": "VAR", "de": "VAR"},
    }
    hour = HourlyForecast.from_raw(item, "nl")
    assert hour.wind_direction is None
    assert hour.wind_direction_text == "VER"


def test_hourly_wind_direction_missing() -> None:
    hour = HourlyForecast.from_raw({"hour": "12"}, "fr")
    assert hour.wind_direction is None
    assert hour.wind_direction_text is None


def test_wind_bearing_helper() -> None:
    from irm_kmi_mcp.models import _wind_bearing

    assert _wind_bearing(None) is None
    assert _wind_bearing("abc") is None
    assert _wind_bearing(248) == 68.0
    assert _wind_bearing(270) == 90.0
    assert _wind_bearing(0) == 180.0
    assert _wind_bearing("45") == 225.0


def test_is_variable_wind() -> None:
    from irm_kmi_mcp.models import _is_variable_wind

    assert _is_variable_wind(None) is False
    assert _is_variable_wind("SE") is False
    assert _is_variable_wind({"fr": "NE"}) is False
    assert _is_variable_wind({"fr": "VAR", "nl": "VER", "en": "VAR"}) is True
    assert _is_variable_wind("var") is True  # case-insensitive


def test_warning_from_forecast() -> None:
    raw = load_fixture("forecast_namur.json")
    w = WeatherWarning.from_forecast(raw["for"]["warning"][0], "fr")
    assert w.type_name == "Chaleur"
    assert w.level == "1"
    assert w.level_label == "yellow"
    assert w.level_color == "yellow"
    assert w.from_timestamp is not None
    assert w.to_timestamp is not None
    assert w.text


def test_warning_from_global() -> None:
    raw = load_fixture("warnings_global.json")
    w = WeatherWarning.from_global(raw[0], "fr")
    assert w.type_name == "Chaleur"
    assert w.icon_country == "BE"
    assert w.level is None  # global feed carries no single level
    assert w.legend_uri and w.legend_uri.startswith("https://app.meteo.be/")
    assert isinstance(w.region, list) and w.region
    first = w.region[0]
    assert first["name"] == "Bruxelles"
    assert first["periods"][0]["level"] == "1"
    assert first["periods"][0]["level_label"] == "yellow"
    assert first["periods"][0]["from"] is not None


def test_normalize_regions_empty() -> None:
    from irm_kmi_mcp.models import _normalize_regions

    assert _normalize_regions(None, "fr") == []
    assert _normalize_regions([{"name": {"fr": "X"}}], "fr") == []


def test_condition_info() -> None:
    from irm_kmi_mcp.constants import condition_info

    assert condition_info(None, "d") == (None, None)
    assert condition_info("0", "d") == (0, "clear sky")
    assert condition_info(0, "n") == (0, "clear night")
    assert condition_info(1, "n") == (1, "partly cloudy night")
    assert condition_info(18, "d") == (18, "rain")
    assert condition_info(99, "d") == (99, "code 99")
    assert condition_info("abc", "d") == (None, "abc")
