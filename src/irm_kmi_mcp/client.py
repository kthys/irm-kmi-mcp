"""Thin client for the undocumented IRM/KMI mobile-app API.

The API lives at ``https://app.meteo.be/services/appv4/`` and is the backend
used by the official IRM mobile app. Every request needs ``s`` (service name)
and ``k`` (a daily MD5 key). The static secret used to derive the key was
reverse-engineered by the community (see ``jdejaegh/irm-kmi-api``) and is
embedded in this client.

Endpoints used:

- ``searchCities``   (param ``n``)  → municipality list with ``ins`` codes
- ``getForecasts``   (param ``ins`` or ``lat``/``long``, ``l``) → obs + daily +
  hourly + warnings + module + rain-radar animation
- ``getWarnings``                  → all active warnings for BE/NL/LU
- ``getSvg``         (full URL from the ``module`` list) → e.g. the pollen SVG
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from .constants import (
    _CACHE_EVICTION_THRESHOLD,
    APP_SECRET,
    BASE_URL,
    CITY_CACHE_TTL,
    DEFAULT_CACHE_TTL,
    DEFAULT_LANG,
    DEFAULT_TIMEOUT,
    RETRY_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    USER_AGENT,
)

logger = logging.getLogger(__name__)


class IrmApiError(RuntimeError):
    """Raised when the IRM API cannot be reached or returns an error."""


@dataclass(frozen=True)
class City:
    """A municipality as returned by ``searchCities``.

    Attributes:
        id: IRM ``ins`` code of the municipality.
        name: Display name, e.g. ``"Namur (BE)"``.
    """

    id: str
    name: str


class IrmApiClient:
    """Synchronous HTTP client for the IRM/KMI mobile-app API.

    Fetches municipality search results, forecasts and warnings. Responses are
    cached in memory: weather data for ``cache_ttl`` seconds and municipality
    lookups for ``city_cache_ttl`` seconds. Cache keys exclude the ``k``
    request parameter. Transient transport errors are retried a few times with
    a short backoff, as the unofficial backend can be flaky.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        city_cache_ttl: float = CITY_CACHE_TTL,
        user_agent: str = USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Initialise the client and its caches.

        Args:
            timeout: HTTP request timeout, in seconds.
            cache_ttl: Time-to-live of the weather-data cache, in seconds.
            city_cache_ttl: Time-to-live of the municipality-lookup cache, in
                seconds.
            user_agent: ``User-Agent`` header sent with every request.
            transport: Optional ``httpx`` transport override.
        """
        self._session = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            transport=transport,
        )
        self._cache_ttl = cache_ttl
        self._city_cache_ttl = city_cache_ttl
        self._data_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._city_cache: dict[tuple[str, str], tuple[float, list[City]]] = {}

    # -- public API -----------------------------------------------------------
    def search_cities(self, query: str, lang: str = DEFAULT_LANG) -> list[City]:
        """Search municipalities by name in Belgium, the Netherlands and Luxembourg.

        Args:
            query: Municipality name or prefix to search for.
            lang: Response language.

        Returns:
            Matching municipalities with their IRM ``ins`` codes.
        """
        cached = self._city_cache.get((query, lang))
        if cached and time.monotonic() - cached[0] < self._city_cache_ttl:
            return cached[1]
        raw = self._get("searchCities", {"n": query, "l": lang})
        cities = [
            City(id=str(item["id"]), name=str(item["name"]))
            for item in raw
            if isinstance(item, dict) and "id" in item and "name" in item
        ]
        self._evict_expired(self._city_cache, self._city_cache_ttl)
        self._city_cache[(query, lang)] = (time.monotonic(), cities)
        return cities

    def get_forecasts(self, ins: str, lang: str = DEFAULT_LANG) -> dict[str, Any]:
        """Fetch observations, forecasts and local warnings for an ``ins`` code.

        Args:
            ins: IRM municipality code.
            lang: Response language.

        Returns:
            The raw ``getForecasts`` response payload.

        Raises:
            IrmApiError: If the response is not a JSON object.
        """
        raw = self._get("getForecasts", {"ins": ins, "l": lang})
        if not isinstance(raw, dict):
            raise IrmApiError(f"Unexpected getForecasts response: {type(raw).__name__}")
        return raw

    def get_forecasts_coord(
        self, latitude: float, longitude: float, lang: str = DEFAULT_LANG
    ) -> dict[str, Any]:
        """Fetch observations, forecasts and local warnings for coordinates.

        Args:
            latitude: WGS84 latitude, rounded to 6 decimals.
            longitude: WGS84 longitude, rounded to 6 decimals.
            lang: Response language.

        Returns:
            The raw ``getForecasts`` response payload.

        Raises:
            IrmApiError: If the response is not a JSON object.
        """
        raw = self._get(
            "getForecasts",
            {"lat": str(round(latitude, 6)), "long": str(round(longitude, 6)), "l": lang},
        )
        if not isinstance(raw, dict):
            raise IrmApiError(f"Unexpected getForecasts response: {type(raw).__name__}")
        return raw

    def get_svg(self, url: str) -> str:
        """Fetch an SVG document published by the IRM API (e.g. pollen).

        The URL is expected to be a full ``app.meteo.be`` service URL as found
        in the ``module`` list of a ``getForecasts`` response (it already
        carries its ``s``/``k`` parameters). Results are cached like weather
        data.

        Args:
            url: Full SVG service URL.

        Returns:
            The SVG document as a string.

        Raises:
            IrmApiError: If the request fails.
        """
        cache_key = ("svg", url)
        now = time.monotonic()
        cached = self._data_cache.get(cache_key)
        if cached and now - cached[0] < self._cache_ttl:
            return cached[1]
        try:
            response = self._request_with_retry(url)
            text = response.text
        except httpx.HTTPError as exc:
            raise IrmApiError(f"IRM SVG request failed: {exc}") from exc
        self._data_cache[cache_key] = (now, text)
        return text

    def get_global_warnings(self, lang: str = DEFAULT_LANG) -> list[dict[str, Any]]:
        """Fetch all active warnings for Belgium, the Netherlands and Luxembourg.

        Args:
            lang: Response language.

        Returns:
            Raw warning items from the ``getWarnings`` endpoint.

        Raises:
            IrmApiError: If the response is not a JSON array.
        """
        raw = self._get("getWarnings", {"l": lang})
        if not isinstance(raw, list):
            raise IrmApiError(f"Unexpected getWarnings response: {type(raw).__name__}")
        return [item for item in raw if isinstance(item, dict)]

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> IrmApiClient:  # noqa: PYI034 (Self needs py3.11+ typing)
        """Return self for use as a context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the underlying HTTP session on exit."""
        self.close()

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _api_key(service: str, when: datetime | None = None) -> str:
        """Derive the daily API key ``md5(secret; service; DD/MM/YYYY)``.

        The date is evaluated in the Europe/Brussels timezone.

        Args:
            service: API service name, e.g. ``"getForecasts"`` or ``"getWarnings"``.
            when: Reference instant used to derive the date. Defaults to now.

        Returns:
            The 32-character MD5 hex digest used as the ``k`` request parameter.
        """
        brussels = ZoneInfo("Europe/Brussels")
        now = (when or datetime.now(brussels)).astimezone(brussels)
        date_str = now.strftime("%d/%m/%Y")
        return hashlib.md5(f"{APP_SECRET};{service};{date_str}".encode()).hexdigest()

    def _get(self, service: str, params: dict[str, str]) -> Any:
        """Send a GET request to the IRM API and return the parsed JSON payload.

        Results are cached by ``(service, params)`` for ``cache_ttl`` seconds.
        The ``s`` and ``k`` query parameters are added automatically.

        Args:
            service: API service name, e.g. ``"getForecasts"``.
            params: Query parameters to append, excluding ``s`` and ``k``.

        Returns:
            The parsed JSON response (shape depends on the service).

        Raises:
            IrmApiError: If the HTTP request fails or the response is not
                valid JSON.
        """
        cache_key = (service, tuple(sorted(params.items())))
        now = time.monotonic()
        cached = self._data_cache.get(cache_key)
        if cached and now - cached[0] < self._cache_ttl:
            return cached[1]

        url = f"{BASE_URL}?{urlencode({**params, 's': service, 'k': self._api_key(service)})}"
        logger.debug("GET %s", url)
        try:
            response = self._request_with_retry(url)
            payload: Any = response.json()
        except httpx.HTTPError as exc:
            raise IrmApiError(f"IRM API request failed ({service}): {exc}") from exc
        except ValueError as exc:  # invalid JSON
            raise IrmApiError(f"IRM API returned invalid JSON ({service}): {exc}") from exc

        self._evict_expired(self._data_cache, self._cache_ttl)
        self._data_cache[cache_key] = (now, payload)
        return payload

    def _request_with_retry(self, url: str) -> httpx.Response:
        """GET ``url``, retrying transient transport errors with a short backoff.

        HTTP status errors are raised immediately (no retry).

        Args:
            url: Absolute URL to fetch.

        Returns:
            The successful :class:`httpx.Response`.

        Raises:
            httpx.HTTPError: When every attempt fails.
        """
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response = self._session.get(url)
                response.raise_for_status()
                return response
            except httpx.TransportError:
                if attempt == RETRY_ATTEMPTS:
                    raise
                logger.debug("Transient IRM API error (attempt %d), retrying", attempt)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _evict_expired(cache: dict[Any, tuple[float, Any]], ttl: float) -> None:
        """Drop expired entries once the cache grows beyond the threshold.

        Args:
            cache: Mapping of key to ``(stored_at, value)`` tuples.
            ttl: Entry time-to-live, in seconds.
        """
        if len(cache) <= _CACHE_EVICTION_THRESHOLD:
            return
        now = time.monotonic()
        for key in [k for k, (stored_at, _) in cache.items() if now - stored_at >= ttl]:
            del cache[key]
