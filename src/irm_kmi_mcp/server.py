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
from .constants import DEFAULT_LANG, MAX_FORECAST_DAYS, MAX_FORECAST_HOURS, SUPPORTED_LANGS
from .models import CurrentConditions, DailyForecast, HourlyForecast, WeatherWarning

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

    def current_conditions(commune: str) -> dict[str, Any]:
        """Return observed weather conditions for a Belgian municipality.

        Conditions come from the nearest IRM weather station. The condition
        label is canonical English, with the raw IRM weather code exposed in
        ``condition_code``.

        Args:
            commune: Municipality name, e.g. "Namur", "Bruxelles", "Ostende".

        Returns:
            Observed conditions as a :class:`CurrentConditions` dict.
        """
        ins = _resolve_ins(commune)
        raw = client.get_forecasts(ins, lang=DEFAULT_LANG)
        uv_data = next(
            (m.get("data") for m in raw.get("module", []) if m.get("type") == "uv"),
            None,
        )
        daily = raw.get("for", {}).get("daily") or [{}]
        sun = {"sunrise": daily[0].get("dawnRiseSeconds"), "sunset": daily[0].get("dawnSetSeconds")}
        return CurrentConditions.from_raw(commune, raw, uv_data, sun).to_dict()

    def daily_forecast(commune: str, days: int = 3, language: str = DEFAULT_LANG) -> list[dict[str, Any]]:
        """Return a daily weather forecast for a Belgian municipality.

        Each day includes the official IRM text bulletin.

        Args:
            commune: Municipality name, e.g. "Namur", "Bruxelles".
            days: Number of days to return, clamped to ``[1, 8]``.
            language: Bulletin language: "fr", "nl", "en" or "de". Defaults
                to the ``IRM_LANG`` environment variable, else English.

        Returns:
            One :class:`DailyForecast` dict per requested day.

        Raises:
            ValueError: If ``language`` is unsupported or ``days`` is non-numeric.
        """
        _validate_language(language)
        days = _coerce_int(days, 1, MAX_FORECAST_DAYS, "days")
        raw = client.get_forecasts(_resolve_ins(commune), lang=language)
        items = raw.get("for", {}).get("daily", [])[:days]
        return [DailyForecast.from_raw(item, language).to_dict() for item in items]

    def hourly_forecast(commune: str, hours: int = 24) -> list[dict[str, Any]]:
        """Return an hourly weather forecast for a Belgian municipality.

        Covers temperature, precipitation, pressure and wind.

        Args:
            commune: Municipality name, e.g. "Namur", "Bruxelles".
            hours: Number of hours to return, clamped to ``[1, 49]``.

        Returns:
            One :class:`HourlyForecast` dict per requested hour.

        Raises:
            ValueError: If ``hours`` is non-numeric.
        """
        hours = _coerce_int(hours, 1, MAX_FORECAST_HOURS, "hours")
        raw = client.get_forecasts(_resolve_ins(commune), lang=DEFAULT_LANG)
        items = raw.get("for", {}).get("hourly", [])[:hours]
        return [HourlyForecast.from_raw(item, DEFAULT_LANG).to_dict() for item in items]

    def warnings(commune: str | None = None, language: str = DEFAULT_LANG) -> list[dict[str, Any]]:
        """Return official IRM weather warnings for Belgium.

        Without ``commune``, returns every active warning for Belgium, the
        Netherlands and Luxembourg. With ``commune``, returns the active
        warnings for that municipality, including the alert level and
        validity period.

        Args:
            commune: Municipality name, e.g. "Namur". Omit for all warnings.
            language: Warning text language: "fr", "nl", "en" or "de".
                Defaults to the ``IRM_LANG`` environment variable, else
                English.

        Returns:
            One :class:`WeatherWarning` dict per active warning.

        Raises:
            ValueError: If ``language`` is unsupported.
        """
        _validate_language(language)
        if commune is None:
            items = client.get_global_warnings(lang=language)
            return [WeatherWarning.from_global(item, language).to_dict() for item in items]
        raw = client.get_forecasts(_resolve_ins(commune), lang=language)
        items = raw.get("for", {}).get("warning", [])
        return [WeatherWarning.from_forecast(item, language).to_dict() for item in items]

    return {
        "current_conditions": current_conditions,
        "daily_forecast": daily_forecast,
        "hourly_forecast": hourly_forecast,
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
            "daily and hourly forecasts, and weather warnings. Municipalities are "
            "resolved by name. Condition labels are canonical English and carry "
            "the raw IRM weather code (condition_code); official texts are "
            "available in fr, nl, en, de via the 'language' parameter (default "
            "from the IRM_LANG environment variable, else English)."
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
