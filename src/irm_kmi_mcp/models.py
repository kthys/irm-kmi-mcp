"""Typed models for the IRM weather data, with raw-API parsing and validation.

The raw API payloads are loosely typed (mixed ``int``/``str``/``None`` values,
per-language dicts, etc.). These dataclasses normalise them into typed Python
objects.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .constants import (
    BRUSSELS_TZ,
    POLLEN_COLOR_LEVELS,
    POLLEN_DOT_RANGES,
    POLLEN_NAMES,
    POLLEN_TEXT_LEVELS,
    WARNING_LEVEL_EN,
    WARNING_TYPE_SLUGS,
    condition_info,
    logger,
)


def _num(value: Any) -> float | None:
    """Coerce a value to ``float``.

    Args:
        value: ``None``, a number, or a numeric string.

    Returns:
        The value as a float, or ``None`` if it cannot be converted.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _lang_text(value: Any, lang: str) -> str | None:
    """Extract the text for ``lang`` from a per-language dict.

    Falls back to the French entry, then to the first available entry.

    Args:
        value: A per-language dict, or a raw value returned unchanged.
        lang: Desired language key.

    Returns:
        The localized text, or ``None`` if no entry is available.
    """
    if not isinstance(value, dict):
        return value
    if lang in value:
        return value[lang]
    if "fr" in value:
        return value["fr"]
    return next(iter(value.values()), None) if value else None


# IRM reports variable wind as "VAR" (fr/en/de) or "VER" (nl); the degrees
# field is then meaningless (it carries a placeholder such as 0).
_VARIABLE_WIND_CODES = frozenset({"VAR", "VER", "VARIABLE"})


def _is_variable_wind(value: Any) -> bool:
    """Tell whether the raw ``windDirectionText`` reports variable wind.

    The raw value is a per-language dict (``{"fr": "VAR", "nl": "VER", ...}``)
    or a plain string; any language entry matching the variable-wind codes is
    enough, so the check is independent of the requested language.

    Args:
        value: Raw ``windDirectionText`` from the API.

    Returns:
        ``True`` when the wind is reported as variable.
    """
    if isinstance(value, dict):
        values = value.values()
    elif value is None:
        return False
    else:
        values = (value,)
    return any(str(v).strip().upper() in _VARIABLE_WIND_CODES for v in values)


def _wind_bearing(degrees: Any) -> float | None:
    """Convert IRM wind degrees to the conventional bearing (direction *from*).

    The undocumented IRM backend reports ``windDirection`` as the direction
    the wind blows *toward*, while ``windDirectionText`` (and the usual
    meteorological convention) express the direction the wind comes *from* —
    the two fields are systematically 180° apart. Rotating by 180° aligns the
    degrees with the text (cross-checked against independent weather sources).
    ``None`` is passed through.

    Args:
        degrees: Raw ``windDirection`` value, or ``None``.

    Returns:
        The conventional bearing in degrees, or ``None``.
    """
    deg = _num(degrees)
    if deg is None:
        return None
    return (deg + 180) % 360


def _hhmm(seconds: Any) -> str | None:
    """Format seconds-since-midnight as ``HH:MM``.

    Args:
        seconds: Seconds since midnight, or ``None``.

    Returns:
        A ``HH:MM`` string, or ``None`` if ``seconds`` is ``None`` or invalid.
    """
    if seconds is None:
        return None
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _iso_timestamp(ts: Any) -> str | None:
    """Normalise a timestamp to an ISO-8601 string.

    Accepts epoch seconds, epoch milliseconds, or an already-formatted string.

    Args:
        ts: A timestamp value, or ``None``.

    Returns:
        An ISO-8601 timestamp string, or ``None``.
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        seconds = ts / 1000 if ts > 1e12 else ts
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    return str(ts)


def _normalize_regions(regions: Any, lang: str) -> list[dict[str, Any]]:
    """Flatten a global-warning region tree into a list of regions.

    Each result region carries its periods with their alert level and validity
    window.

    Args:
        regions: Raw ``region`` payload from a global warning item.
        lang: Language used for the region names.

    Returns:
        A list of ``{"name", "periods"}`` dicts, or an empty list when
        ``regions`` is empty or contains no valid periods.
    """
    result: list[dict[str, Any]] = []
    for region in regions or []:
        if not isinstance(region, dict):
            continue
        periods: list[dict[str, Any]] = []
        for district in region.get("district") or []:
            for interval in district.get("intervals") or []:
                level = str(interval["level"]) if interval.get("level") is not None else None
                periods.append(
                    {
                        "level": level,
                        "level_label": WARNING_LEVEL_EN.get(level or "") if level else None,
                        "from": _iso_timestamp(interval.get("fromTimestamp")),
                        "to": _iso_timestamp(interval.get("toTimestamp")),
                    }
                )
        if periods:
            result.append({"name": _lang_text(region.get("name"), lang), "periods": periods})
    return result


def _prune(value: Any) -> Any:
    """Recursively drop ``None`` and empty-string fields from a serialised model.

    Keeping null fields costs LLM context tokens without adding information;
    absent fields read naturally as "unknown".

    Args:
        value: A dict, list, or scalar produced by ``asdict``.

    Returns:
        The same structure without ``None``/empty-string leaves (and without
        dicts/lists that became empty as a result).
    """
    if isinstance(value, dict):
        pruned = {k: _prune(v) for k, v in value.items() if v is not None and v != ""}
        return {k: v for k, v in pruned.items() if v != {} and v != []}
    if isinstance(value, list):
        return [_prune(item) for item in value]
    return value


def hourly_times(items: list[dict[str, Any]], now: datetime | None = None) -> list[str | None]:
    """Compute absolute ISO-8601 timestamps for raw hourly-forecast entries.

    The API only reports the hour of day (``hour``) and marks day boundaries
    with ``dateShow`` (``DD/MM``). The first entry is anchored to the current
    hour in ``Europe/Brussels``; subsequent entries advance one hour at a
    time, with ``dateShow`` correcting the date when present.

    Args:
        items: Raw entries from the ``for.hourly`` list.
        now: Reference instant (defaults to the current time).

    Returns:
        One ISO-8601 timestamp (minute precision, Brussels offset) per entry;
        ``None`` for entries whose hour cannot be parsed.
    """
    tz = ZoneInfo(BRUSSELS_TZ)
    now = now or datetime.now(tz)
    times: list[str | None] = []
    current: datetime | None = None
    for index, item in enumerate(items):
        try:
            hour = int(item.get("hour"))
        except (TypeError, ValueError):
            times.append(None)
            continue
        if index == 0:
            current = datetime.combine(now.date(), datetime.min.time(), tzinfo=tz).replace(
                hour=hour
            )
            # The first entry is the current (or a near) hour; if anchoring it
            # on today lands clearly in the past, it belongs to tomorrow.
            if current <= now - timedelta(hours=2):
                current += timedelta(days=1)
        else:
            assert current is not None
            current = current + timedelta(hours=1)
            date_show = item.get("dateShow")
            if isinstance(date_show, str) and "/" in date_show:
                try:
                    day, month = (int(part) for part in date_show.split("/", 1))
                except ValueError:
                    day = month = None
                if day is not None and (current.day, current.month) != (day, month):
                    year = current.year + (1 if current.month == 12 and month == 1 else 0)
                    current = datetime(year, month, day, hour, tzinfo=tz)
        times.append(current.isoformat(timespec="minutes"))
    return times


def pollen_from_svg(svg: str) -> dict[str, str] | None:
    """Extract per-species pollen levels from the IRM pollen SVG.

    Ports the parser from ``jdejaegh/irm-kmi-api``: pollen names and level
    words are read from ``tspan`` elements aligned on the x axis; when a name
    reads "active" (or carries no word), the level dot's position on the color
    scale decides the level.

    Args:
        svg: The pollen SVG document as a string.

    Returns:
        Mapping of pollen name to level word (none, active, low, moderate,
        high, very high), or ``None`` when the SVG cannot be parsed.
    """
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        logger.warning("Could not parse pollen SVG: %s", exc)
        return None

    elements: list[ET.Element] = []

    def _collect(node: ET.Element) -> None:
        for child in node:
            elements.append(child)
            _collect(child)

    _collect(root)

    def _text(element: ET.Element) -> str | None:
        return element.text.strip() if element.text else None

    pollens: dict[str | None, str] = {}
    levels: dict[str | None, str] = {}
    dot_positions: list[float] = []
    for element in elements:
        if "tspan" in element.tag:
            text = _text(element)
            if text is None:
                continue
            lowered = text.lower()
            if lowered in POLLEN_NAMES:
                pollens[element.attrib.get("x")] = lowered
            elif lowered in POLLEN_TEXT_LEVELS:
                levels[element.attrib.get("x")] = POLLEN_TEXT_LEVELS[lowered]
        elif "circle" in element.tag:
            try:
                dot_positions.append(float(element.attrib["cx"]))
            except (KeyError, ValueError):
                continue

    if not pollens:
        return None

    result: dict[str, str] = {}
    for position, name in pollens.items():
        level = levels.get(position) if position is not None else None
        if level in (None, "active", "none"):
            # No decisive word below the name: locate the level dot relative
            # to the pollen label on the color scale.
            for dot in dot_positions:
                if position is None:
                    break
                try:
                    relative = float(position) - dot
                except TypeError:
                    continue
                for lo, hi, color in POLLEN_DOT_RANGES:
                    if lo <= relative <= hi:
                        level = POLLEN_COLOR_LEVELS[color]
                        break
                if level not in (None, "active", "none"):
                    break
        result[name] = level or "active"
    return result


def rain_nowcast(raw: dict[str, Any], lang: str) -> dict[str, Any] | None:
    """Build a compact rain nowcast from the ``animation`` radar sequence.

    The ``getForecasts`` payload embeds a ~5-hour radar precipitation
    sequence (frames every 10 minutes). Frame values are converted to mm
    using the sequence's own value/position ratio, as done by
    ``jdejaegh/irm-kmi-api``. Only frames expecting precipitation are kept,
    to keep tool output small.

    Args:
        raw: Raw ``getForecasts`` payload.
        lang: Language for the unit and the official IRM hint.

    Returns:
        A dict with ``unit``, the official ``hint`` (when available) and a
        ``frames`` list of ``{"time", "precip_mm"}``; ``None`` when the
        payload carries no animation sequence.
    """
    animation = raw.get("animation")
    if not isinstance(animation, dict):
        return None
    sequence = animation.get("sequence")
    if not isinstance(sequence, list) or not sequence:
        return None

    ratios: list[float] = []
    for frame in sequence:
        if not isinstance(frame, dict):
            continue
        value, position = _num(frame.get("value")), _num(frame.get("position"))
        if value is not None and position is not None and position > 0:
            ratios.append(value / position)
    ratio = mean(ratios) if ratios else 0.0

    frames: list[dict[str, Any]] = []
    for frame in sequence:
        if not isinstance(frame, dict):
            continue
        value = _num(frame.get("value"))
        position_higher = _num(frame.get("positionHigher")) or 0.0
        precip = max(value or 0.0, position_higher * ratio)
        if precip <= 0:
            continue
        time_str = str(frame.get("time") or "")
        frames.append(
            {
                "time": time_str[:16],  # "2026-08-12T17:00" (local, offset dropped)
                "precip_mm": round(precip, 2),
            }
        )

    result: dict[str, Any] = {
        "unit": _lang_text(animation.get("unit"), lang) or "mm/10min",
        "frames": frames,
    }
    hint = _lang_text(animation.get("sequenceHint"), lang)
    if hint:
        result["hint"] = hint
    return result


@dataclass
class CurrentConditions:
    """Observed conditions at the nearest IRM weather station.

    Attributes:
        city_name: Municipality name returned by the API.
        observation_time: Observation timestamp.
        temperature_c: Temperature in degrees Celsius.
        condition: Canonical English condition label.
        condition_code: Raw IRM weather code (ww) for the condition.
        day_night: ``"d"`` for day or ``"n"`` for night.
        uv_index: UV index value.
        uv_level: UV level label (canonical English).
        sunrise: Sunrise time as ``HH:MM``.
        sunset: Sunset time as ``HH:MM``.
    """

    city_name: str
    observation_time: str | None
    temperature_c: float | None
    condition: str | None
    condition_code: int | None
    day_night: str | None
    uv_index: float | None
    uv_level: str | None
    sunrise: str | None
    sunset: str | None

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        uv_data: dict[str, Any] | None = None,
        sun: dict[str, Any] | None = None,
    ) -> CurrentConditions:
        """Build observed conditions from a ``getForecasts`` response.

        Args:
            raw: Raw ``getForecasts`` payload containing the ``obs`` block.
            uv_data: UV module ``data`` block, or ``None`` if absent.
            sun: Dict with ``sunrise`` and ``sunset`` seconds-since-midnight,
                or ``None`` if unknown.

        Returns:
            A populated :class:`CurrentConditions`.
        """
        obs = raw.get("obs") or {}
        sun = sun or {}
        condition_code, condition = condition_info(obs.get("ww"), obs.get("dayNight"))
        return cls(
            city_name=str(raw.get("cityName") or ""),
            observation_time=obs.get("timestamp"),
            temperature_c=_num(obs.get("temp")),
            condition=condition,
            condition_code=condition_code,
            day_night=obs.get("dayNight"),
            uv_index=_num((uv_data or {}).get("levelValue")),
            uv_level=_lang_text((uv_data or {}).get("level"), "en"),
            sunrise=_hhmm(sun.get("sunrise")),
            sunset=_hhmm(sun.get("sunset")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the conditions as a plain dict, without empty fields."""
        return _prune(asdict(self))


@dataclass
class DailyForecast:
    """One day of the IRM daily forecast, including the official text bulletin.

    Attributes:
        day_name: Localized day name.
        period: Forecast period code.
        temp_min_c: Minimum temperature in degrees Celsius.
        temp_max_c: Maximum temperature in degrees Celsius.
        condition_day: Canonical English day condition label.
        condition_day_code: Raw IRM weather code (ww) for the day.
        condition_night: Canonical English night condition label.
        condition_night_code: Raw IRM weather code (ww) for the night.
        wind_kmh: Wind speed in km/h.
        wind_peak_kmh: Peak wind speed in km/h.
        wind_direction: Localized wind direction text.
        precip_chance_pct: Precipitation probability, in percent.
        precip_mm: Precipitation amount, in mm.
        sunrise: Sunrise time as ``HH:MM``.
        sunset: Sunset time as ``HH:MM``.
        text: Official IRM text bulletin.
    """

    day_name: str | None
    period: str | None
    temp_min_c: float | None
    temp_max_c: float | None
    condition_day: str | None
    condition_day_code: int | None
    condition_night: str | None
    condition_night_code: int | None
    wind_kmh: float | None
    wind_peak_kmh: float | None
    wind_direction: str | None
    precip_chance_pct: float | None
    precip_mm: float | None
    sunrise: str | None
    sunset: str | None
    text: str | None

    @classmethod
    def from_raw(cls, item: dict[str, Any], lang: str) -> DailyForecast:
        """Build one day of forecast from a daily-forecast list item.

        Args:
            item: A raw entry from the ``for.daily`` list.
            lang: Language used for the text, day name and wind direction.

        Returns:
            A populated :class:`DailyForecast`.
        """
        wind = item.get("wind") or {}
        day_code, day_label = condition_info(item.get("ww1"), "d")
        night_code, night_label = condition_info(item.get("ww2"), "n")
        return cls(
            day_name=_lang_text(item.get("dayName"), lang),
            period=item.get("period"),
            temp_min_c=_num(item.get("tempMin")),
            temp_max_c=_num(item.get("tempMax")),
            condition_day=day_label,
            condition_day_code=day_code,
            condition_night=night_label,
            condition_night_code=night_code,
            wind_kmh=_num(wind.get("speed")),
            wind_peak_kmh=_num(wind.get("peakSpeed")),
            wind_direction=_lang_text(wind.get("dirText"), lang),
            precip_chance_pct=_num(item.get("precipChance")),
            precip_mm=_num(item.get("precipQuantity")),
            sunrise=_hhmm(item.get("dawnRiseSeconds")),
            sunset=_hhmm(item.get("dawnSetSeconds")),
            text=_lang_text(item.get("text"), lang),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the day forecast as a plain dict, without empty fields."""
        return _prune(asdict(self))


@dataclass
class HourlyForecast:
    """One hour of the IRM hourly forecast.

    Attributes:
        time: ISO-8601 timestamp of the forecasted hour (Brussels time).
        temperature_c: Temperature in degrees Celsius.
        condition: Canonical English condition label.
        condition_code: Raw IRM weather code (ww) for the condition.
        precip_chance_pct: Precipitation probability, in percent.
        precip_mm: Precipitation amount, in mm.
        pressure_hpa: Atmospheric pressure in hPa.
        wind_kmh: Wind speed in km/h.
        wind_gust_kmh: Wind gust speed in km/h.
        wind_direction: Wind direction in degrees, as the direction the wind
            comes *from* (meteorological convention); ``None`` when unknown or
            when the wind is variable.
        wind_direction_text: Localized wind direction text.
    """

    time: str | None
    temperature_c: float | None
    condition: str | None
    condition_code: int | None
    precip_chance_pct: float | None
    precip_mm: float | None
    pressure_hpa: float | None
    wind_kmh: float | None
    wind_gust_kmh: float | None
    wind_direction: float | None
    wind_direction_text: str | None

    @classmethod
    def from_raw(
        cls, item: dict[str, Any], lang: str, time: str | None = None
    ) -> HourlyForecast:
        """Build one hour of forecast from an hourly-forecast list item.

        Args:
            item: A raw entry from the ``for.hourly`` list.
            lang: Language used for the wind-direction text.
            time: Absolute ISO-8601 timestamp for the entry, as computed by
                :func:`hourly_times`.

        Returns:
            A populated :class:`HourlyForecast`.
        """
        condition_code, condition = condition_info(item.get("ww"), item.get("dayNight"))
        wind_text_raw = item.get("windDirectionText")
        return cls(
            time=time or str(item.get("hour")),
            temperature_c=_num(item.get("temp")),
            condition=condition,
            condition_code=condition_code,
            precip_chance_pct=_num(item.get("precipChance")),
            precip_mm=_num(item.get("precipQuantity")),
            pressure_hpa=_num(item.get("pressure")),
            wind_kmh=_num(item.get("windSpeedKm")),
            wind_gust_kmh=_num(item.get("windPeakSpeedKm")),
            wind_direction=(
                None
                if _is_variable_wind(wind_text_raw)
                else _wind_bearing(item.get("windDirection"))
            ),
            wind_direction_text=_lang_text(wind_text_raw, lang),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the hour forecast as a plain dict, without empty fields."""
        return _prune(asdict(self))


@dataclass
class WeatherWarning:
    """An official IRM weather warning (yellow/orange/red).

    Attributes:
        type_id: Warning type identifier.
        type: Canonical English warning-type slug (e.g. ``"heat"``, ``"wind"``).
        type_name: Localized warning type name.
        level: Warning level code (``"0"``-``"3"``), or ``None`` for global
            warnings.
        level_label: Canonical English level label, or ``None`` for global
            warnings.
        region: Flattened per-region periods for global warnings, or ``None``
            for municipality warnings.
        from_timestamp: Warning start, as an ISO-8601 string.
        to_timestamp: Warning end, as an ISO-8601 string.
        text: Localized warning text.
        icon_country: Country code of the warning icon.
    """

    type_id: str | None
    type: str | None
    type_name: str | None
    level: str | None
    level_label: str | None
    region: list[dict[str, Any]] | str | None
    from_timestamp: str | None
    to_timestamp: str | None
    text: str | None
    icon_country: str | None

    @classmethod
    def from_forecast(cls, item: dict[str, Any], lang: str) -> WeatherWarning:
        """Build a warning from a ``getForecasts`` warning item.

        Args:
            item: A raw warning entry from a ``getForecasts`` response.
            lang: Language used for the warning text and type name.

        Returns:
            A :class:`WeatherWarning` with its level and validity window set.
        """
        return cls(
            type_id=str(item["warningType"]["id"]),
            type=WARNING_TYPE_SLUGS.get(str(item["warningType"]["id"])),
            type_name=_lang_text(item["warningType"].get("name"), lang),
            level=str(item.get("warningLevel")) if item.get("warningLevel") is not None else None,
            level_label=WARNING_LEVEL_EN.get(str(item.get("warningLevel") or "")) if item.get("warningLevel") is not None else None,
            region=None,
            from_timestamp=_iso_timestamp(item.get("fromTimestamp")),
            to_timestamp=_iso_timestamp(item.get("toTimestamp")),
            text=_lang_text(item.get("text"), lang),
            icon_country=item.get("icon_country"),
        )

    @classmethod
    def from_global(cls, item: dict[str, Any], lang: str) -> WeatherWarning:
        """Build a warning from a ``getWarnings`` item.

        Leaves ``level`` and ``level_label`` as ``None`` and populates ``region``
        with the flattened per-region periods.

        Args:
            item: A raw warning entry from the global ``getWarnings`` response.
            lang: Language used for the warning text and type name.

        Returns:
            A :class:`WeatherWarning` with aggregate per-region periods.
        """
        return cls(
            type_id=str(item["warningType"]["id"]),
            type=WARNING_TYPE_SLUGS.get(str(item["warningType"]["id"])),
            type_name=_lang_text(item["warningType"].get("name"), lang),
            level=None,
            level_label=None,
            region=_normalize_regions(item.get("region"), lang),
            from_timestamp=None,
            to_timestamp=None,
            text=_lang_text(item.get("text"), lang),
            icon_country=item.get("icon_country"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the warning as a plain dict, without empty fields."""
        return _prune(asdict(self))
