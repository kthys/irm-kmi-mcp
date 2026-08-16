"""FastMCP server exposing official Belgian weather data (IRM/KMI).

Run standalone (stdio) with ``irm-kmi-mcp`` or ``python -m irm_kmi_mcp``,
or embed the server in your own application with :func:`create_server`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from . import __version__
from .client import IrmApiClient
from .constants import (
    DEFAULT_LANG,
    DEFAULT_POLLEN_COMMUNE,
    MAX_FORECAST_DAYS,
    MAX_FORECAST_HOURS,
    SUPPORTED_COUNTRIES,
    SUPPORTED_LANGS,
)
from .models import (
    CurrentConditions,
    DailyForecast,
    HourlyForecast,
    WeatherWarning,
    hourly_times,
    pollen_from_svg,
    rain_nowcast,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "irm-kmi-mcp"


def _validate_language(language: str) -> None:
    """Validate that ``language`` is a supported IRM API language.

    Args:
        language: Language code to validate.

    Raises:
        ValueError: If ``language`` is not one of the supported languages.
    """
    if language not in SUPPORTED_LANGS:
        raise ValueError(
            f"Unsupported language: {language!r}. Supported values: {', '.join(SUPPORTED_LANGS)}."
        )


def _validate_country(country: str) -> str:
    """Validate and normalise a warning country filter.

    Args:
        country: Country code to validate (case-insensitive).

    Returns:
        The upper-case country code.

    Raises:
        ValueError: If ``country`` is not one of the supported countries.
    """
    normalised = country.strip().upper()
    if normalised not in SUPPORTED_COUNTRIES:
        raise ValueError(
            f"Unsupported country: {country!r}. Supported values: {', '.join(SUPPORTED_COUNTRIES)}."
        )
    return normalised


def _validate_coordinates(latitude: float | None, longitude: float | None) -> None:
    """Validate WGS84 coordinates.

    Args:
        latitude: Latitude in degrees.
        longitude: Longitude in degrees.

    Raises:
        ValueError: If either coordinate is out of range.
    """
    if not (-90 <= latitude <= 90):  # type: ignore[operator]
        raise ValueError(f"latitude must be between -90 and 90, got {latitude!r}.")
    if not (-180 <= longitude <= 180):  # type: ignore[operator]
        raise ValueError(f"longitude must be between -180 and 180, got {longitude!r}.")


def _coerce_int(value: object, lo: int, hi: int, name: str) -> int:
    """Coerce ``value`` to an int and clamp it to the inclusive range ``[lo, hi]``.

    Out-of-range integers are clamped to the nearest bound.

    Args:
        value: Value to coerce.
        lo: Inclusive lower bound.
        hi: Inclusive upper bound.
        name: Parameter name used in the error message.

    Returns:
        The coerced integer, clamped to ``[lo, hi]``.

    Raises:
        ValueError: If ``value`` cannot be converted to ``int``.
    """
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer between {lo} and {hi}.") from exc
    return max(lo, min(n, hi))


def build_tools(client: IrmApiClient) -> dict[str, Callable[..., Any]]:
    """Build the tool functions bound to an :class:`IrmApiClient`.

    Args:
        client: Client used to resolve municipalities and fetch weather data.

    Returns:
        Mapping of tool name to callable.
    """

    def _resolve_ins(commune: str) -> str:
        """Resolve a municipality name to its IRM ``ins`` code via the client.

        Args:
            commune: Municipality name to resolve.

        Returns:
            The IRM ``ins`` code of the first matching Belgian municipality.

        Raises:
            ValueError: If no municipality matches ``commune``.
        """
        cities = client.search_cities(commune)
        if not cities:
            raise ValueError(
                f"Municipality not found: {commune!r}. Try a different spelling "
                "(e.g. 'Bruxelles', 'Namur')."
            )
        belgian = next((c for c in cities if c.name.endswith("(BE)")), cities[0])
        return belgian.id

    def _fetch_forecasts(
        commune: str | None,
        latitude: float | None,
        longitude: float | None,
        lang: str,
    ) -> dict[str, Any]:
        """Fetch the raw ``getForecasts`` payload for a location.

        Accepts either a municipality name or WGS84 coordinates.

        Args:
            commune: Municipality name, or ``None`` when coordinates are used.
            latitude: WGS84 latitude, or ``None`` when a name is used.
            longitude: WGS84 longitude, or ``None`` when a name is used.
            lang: Response language.

        Returns:
            The raw ``getForecasts`` response payload.

        Raises:
            ValueError: If neither (or only partially) a location is given.
        """
        if latitude is not None and longitude is not None:
            _validate_coordinates(latitude, longitude)
            return client.get_forecasts_coord(latitude, longitude, lang=lang)
        if commune:
            return client.get_forecasts(_resolve_ins(commune), lang=lang)
        raise ValueError("Provide either 'commune', or both 'latitude' and 'longitude'.")

    def current_conditions(
        commune: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> dict[str, Any]:
        """Return observed weather conditions for a location in Belgium.

        Conditions come from the nearest IRM weather station. The condition
        label is canonical English, with the raw IRM weather code exposed in
        ``condition_code``.

        Args:
            commune: Municipality name, e.g. "Namur", "Bruxelles", "Ostende".
            latitude: WGS84 latitude; use with ``longitude`` instead of
                ``commune``.
            longitude: WGS84 longitude; use with ``latitude`` instead of
                ``commune``.

        Returns:
            Observed conditions as a :class:`CurrentConditions` dict.
        """
        raw = _fetch_forecasts(commune, latitude, longitude, lang=DEFAULT_LANG)
        uv_data = next(
            (m.get("data") for m in raw.get("module", []) if m.get("type") == "uv"),
            None,
        )
        daily = raw.get("for", {}).get("daily") or [{}]
        sun = {"sunrise": daily[0].get("dawnRiseSeconds"), "sunset": daily[0].get("dawnSetSeconds")}
        return CurrentConditions.from_raw(raw, uv_data, sun).to_dict()

    def daily_forecast(
        commune: str | None = None,
        days: int = 3,
        language: str = DEFAULT_LANG,
        latitude: float | None = None,
        longitude: float | None = None,
        include_text: bool = True,
    ) -> list[dict[str, Any]]:
        """Return a daily weather forecast for a location in Belgium.

        Each day includes the official IRM text bulletin unless
        ``include_text`` is false.

        Args:
            commune: Municipality name, e.g. "Namur", "Bruxelles".
            days: Number of days to return, clamped to ``[1, 8]``.
            language: Bulletin language: "fr", "nl", "en" or "de". Defaults
                to the ``IRM_LANG`` environment variable, else English.
            latitude: WGS84 latitude; use with ``longitude`` instead of
                ``commune``.
            longitude: WGS84 longitude; use with ``latitude`` instead of
                ``commune``.
            include_text: Set to false to omit the (long) official bulletin
                text and save tokens.

        Returns:
            One :class:`DailyForecast` dict per requested day.

        Raises:
            ValueError: If ``language`` is unsupported or ``days`` is non-numeric.
        """
        _validate_language(language)
        days = _coerce_int(days, 1, MAX_FORECAST_DAYS, "days")
        raw = _fetch_forecasts(commune, latitude, longitude, lang=language)
        items = raw.get("for", {}).get("daily", [])[:days]
        result = [DailyForecast.from_raw(item, language).to_dict() for item in items]
        if not include_text:
            for day in result:
                day.pop("text", None)
        return result

    def hourly_forecast(
        commune: str | None = None,
        hours: int = 24,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return an hourly weather forecast for a location in Belgium.

        Covers temperature, precipitation, pressure and wind. Every row carries
        an absolute ``time`` (ISO-8601, Brussels time).

        Args:
            commune: Municipality name, e.g. "Namur", "Bruxelles".
            hours: Number of hours to return, clamped to ``[1, 49]``.
            latitude: WGS84 latitude; use with ``longitude`` instead of
                ``commune``.
            longitude: WGS84 longitude; use with ``latitude`` instead of
                ``commune``.

        Returns:
            One :class:`HourlyForecast` dict per requested hour.

        Raises:
            ValueError: If ``hours`` is non-numeric.
        """
        hours = _coerce_int(hours, 1, MAX_FORECAST_HOURS, "hours")
        raw = _fetch_forecasts(commune, latitude, longitude, lang=DEFAULT_LANG)
        items = raw.get("for", {}).get("hourly", [])[:hours]
        times = hourly_times(items)
        return [
            HourlyForecast.from_raw(item, DEFAULT_LANG, time=time).to_dict()
            for item, time in zip(items, times)
        ]

    def rain_forecast(
        commune: str | None = None,
        language: str = DEFAULT_LANG,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> dict[str, Any]:
        """Return the short-term rain nowcast for a location in Belgium.

        Based on the IRM rain radar: expected precipitation per 10-minute
        frame over roughly the next 5 hours. Answers questions such as
        "will it rain in the next hour?" much more cheaply than the hourly
        forecast.

        Args:
            commune: Municipality name, e.g. "Namur", "Bruxelles".
            language: Language for the unit and official hint: "fr", "nl",
                "en" or "de".
            latitude: WGS84 latitude; use with ``longitude`` instead of
                ``commune``.
            longitude: WGS84 longitude; use with ``latitude`` instead of
                ``commune``.

        Returns:
            A dict with ``unit``, the official IRM ``hint`` and ``frames``,
            a list of ``{"time", "precip_mm"}`` for frames expecting
            precipitation (empty when no rain is expected).

        Raises:
            ValueError: If ``language`` is unsupported.
        """
        _validate_language(language)
        raw = _fetch_forecasts(commune, latitude, longitude, lang=language)
        result = rain_nowcast(raw, language)
        if result is None:
            return {"frames": [], "hint": "No radar data available for this location."}
        return result

    def pollen(
        commune: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> dict[str, Any]:
        """Return today's official IRM pollen levels for Belgium.

        Pollen levels are national; the location only selects the forecast
        payload they are read from and defaults to Brussels.

        Args:
            commune: Municipality name; defaults to "Bruxelles".
            latitude: WGS84 latitude; use with ``longitude`` instead of
                ``commune``.
            longitude: WGS84 longitude; use with ``latitude`` instead of
                ``commune``.

        Returns:
            ``{"available": true, "levels": {name: level}}`` with levels
            among none, active, low, moderate, high, very high; or
            ``{"available": false}`` when the IRM publishes no pollen data
            (typically outside the pollen season).
        """
        raw = _fetch_forecasts(
            commune or DEFAULT_POLLEN_COMMUNE, latitude, longitude, lang=DEFAULT_LANG
        )
        svg_url: str | None = None
        for module in raw.get("module", []):
            if module.get("type") != "svg":
                continue
            url = (module.get("data") or {}).get("url") or {}
            candidate = url.get(DEFAULT_LANG) or url.get("en") or next(iter(url.values()), None)
            if candidate and "pollen" in candidate:
                svg_url = candidate
                break
        if svg_url is None:
            return {"available": False}
        levels = pollen_from_svg(client.get_svg(svg_url))
        if not levels:
            return {"available": False}
        return {"available": True, "levels": levels}

    def warnings(
        commune: str | None = None,
        language: str = DEFAULT_LANG,
        country: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return official IRM weather warnings for Belgium.

        Without ``commune``, returns every active warning for Belgium, the
        Netherlands and Luxembourg (filterable with ``country``). With
        ``commune`` (or coordinates), returns the active warnings for that
        municipality, including the alert level and validity period.

        Args:
            commune: Municipality name, e.g. "Namur". Omit for all warnings.
            language: Warning text language: "fr", "nl", "en" or "de".
                Defaults to the ``IRM_LANG`` environment variable, else
                English.
            country: Only for the country-wide listing: restrict to "BE",
                "NL" or "LU".
            latitude: WGS84 latitude; use with ``longitude`` instead of
                ``commune``.
            longitude: WGS84 longitude; use with ``latitude`` instead of
                ``commune``.

        Returns:
            One :class:`WeatherWarning` dict per active warning.

        Raises:
            ValueError: If ``language`` or ``country`` is unsupported.
        """
        _validate_language(language)
        if commune is None and latitude is None and longitude is None:
            items = client.get_global_warnings(lang=language)
            if country is not None:
                wanted = _validate_country(country)
                items = [item for item in items if item.get("icon_country") == wanted]
            return [WeatherWarning.from_global(item, language).to_dict() for item in items]
        if country is not None:
            raise ValueError("'country' only applies when no location is given.")
        raw = _fetch_forecasts(commune, latitude, longitude, lang=language)
        items = raw.get("for", {}).get("warning", [])
        return [WeatherWarning.from_forecast(item, language).to_dict() for item in items]

    return {
        "current_conditions": current_conditions,
        "daily_forecast": daily_forecast,
        "hourly_forecast": hourly_forecast,
        "rain_forecast": rain_forecast,
        "pollen": pollen,
        "warnings": warnings,
    }


def create_server(client: IrmApiClient | None = None) -> FastMCP:
    """Create and return the FastMCP server with all tools registered.

    Args:
        client: Pre-built client to bind to the tools. A new default client is
            created when omitted.

    Returns:
        The configured :class:`FastMCP` server.
    """
    client = client or IrmApiClient()
    mcp = FastMCP(
        SERVER_NAME,
        version=__version__,
        instructions=(
            "Official Belgian weather data from the IRM/KMI: observed conditions, "
            "daily and hourly forecasts, a radar-based rain nowcast, pollen "
            "levels and weather warnings. Locations are given as a municipality "
            "name ('commune') or as WGS84 coordinates ('latitude'/'longitude'). "
            "Condition labels are canonical English and carry the raw IRM "
            "weather code (condition_code); official texts are available in "
            "fr, nl, en, de via the 'language' parameter (default from the "
            "IRM_LANG environment variable, else English). Prefer rain_forecast "
            "over hourly_forecast for 'will it rain soon' questions, and pass "
            "include_text=false to daily_forecast when only numbers are needed."
        ),
    )
    for name, fn in build_tools(client).items():
        mcp.tool(name=name)(fn)
    return mcp


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    create_server().run()


if __name__ == "__main__":
    main()
