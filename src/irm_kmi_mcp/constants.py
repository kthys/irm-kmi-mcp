"""Constants for the IRM weather MCP server.

The IRM/KMI exposes an undocumented JSON API used by its official mobile app
(``https://app.meteo.be/services/appv4/``). The authentication scheme (a daily
MD5 key derived from a static secret) was reverse-engineered by the community;
see the README for attribution and caveats.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# --- API ---------------------------------------------------------------------
BASE_URL = "https://app.meteo.be/services/appv4/"
APP_SECRET = "r9EnW374jkJ9acc"
USER_AGENT = "kthys/irm-kmi-mcp"

# --- Behaviour ---------------------------------------------------------------
SUPPORTED_LANGS = ("fr", "nl", "en", "de")


def _env_default_lang() -> str:
    """Resolve the default language from the ``IRM_LANG`` environment variable.

    Falls back to English when the variable is unset or holds an unsupported
    value (a warning is logged in the latter case).

    Returns:
        A supported language code ("fr", "nl", "en" or "de").
    """
    value = os.environ.get("IRM_LANG", "").strip().lower()
    if value in SUPPORTED_LANGS:
        return value
    if value:
        logger.warning(
            "Ignoring unsupported IRM_LANG=%r; falling back to 'en'. "
            "Supported values: %s.",
            value,
            ", ".join(SUPPORTED_LANGS),
        )
    return "en"


DEFAULT_LANG = _env_default_lang()
DEFAULT_CACHE_TTL = 600          # seconds: weather data cache
CITY_CACHE_TTL = 86_400          # seconds: municipality lookup cache (24 h)
DEFAULT_TIMEOUT = 20             # seconds: HTTP timeout
MAX_FORECAST_DAYS = 8
MAX_FORECAST_HOURS = 49

# --- IRM weather codes (ww 0-27) → canonical English labels -------------------
# The ww-code → condition mapping mirrors the one in the irm-kmi-ha Home
# Assistant integration (MIT, jdejaegh/irm-kmi-ha); the English labels match
# the official IRM/meteo.be wording. Labels are canonical: MCP clients are
# LLMs, which localize presentation themselves — the raw code is always
# exposed alongside the label (see condition_info).
WW_EN: dict[int, str] = {
    0: "clear sky",
    1: "partly cloudy",
    2: "thunderstorm",
    3: "partly cloudy",
    4: "heavy rain",
    5: "thundery showers",
    6: "heavy rain",
    7: "thundery showers",
    8: "mixed rain and snow",
    9: "mixed rain and snow",
    10: "thunderstorms",
    11: "snow",
    12: "snow",
    13: "thunderstorms",
    14: "overcast",
    15: "overcast",
    16: "heavy rain",
    17: "thundery showers",
    18: "rain",
    19: "heavy rain",
    20: "mixed rain and snow",
    21: "rain",
    22: "snow",
    23: "snow",
    24: "fog",
    25: "fog",
    26: "fog",
    27: "fog",
}

# Clear-sky codes get a night variant when the API reports night time.
WW_NIGHT_EN: dict[int, str] = {
    0: "clear night",
    1: "partly cloudy night",
}


def condition_info(
    ww: int | str | None, day_night: str | None
) -> tuple[int | None, str | None]:
    """Map an IRM weather code to its raw code and canonical English label.

    Args:
        ww: IRM weather code, given as an int, string, or ``None``.
        day_night: ``"d"`` for day or ``"n"`` for night.

    Returns:
        A ``(code, label)`` tuple; ``label`` applies the night variant when
        applicable. Both entries are ``None`` when ``ww`` is ``None``. Unknown
        codes keep their numeric code with a ``"code N"`` label.
    """
    if ww is None:
        return None, None
    try:
        code = int(ww)
    except (TypeError, ValueError):
        return None, str(ww)
    if day_night == "n" and code in WW_NIGHT_EN:
        return code, WW_NIGHT_EN[code]
    return code, WW_EN.get(code, f"code {code}")


# --- Warning taxonomy ---------------------------------------------------------
# Warning level codes → canonical English labels. The level colors are also
# exposed (WARNING_LEVEL_COLOR); warning type names come from the API itself,
# localized per request language.
WARNING_LEVEL_EN: dict[str, str] = {
    "0": "none",
    "1": "yellow",
    "2": "orange",
    "3": "red",
}

WARNING_LEVEL_COLOR: dict[str, str] = {
    "0": "green",
    "1": "yellow",
    "2": "orange",
    "3": "red",
}
