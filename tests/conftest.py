"""Shared fixtures: load recorded API responses and build a mocked client."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from irm_kmi_mcp.client import IrmApiClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_handler(
    search_result: list[dict[str, str]] | None = None,
    error: Exception | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Route requests by service name, backed by recorded fixtures."""

    def handler(request: httpx.Request) -> httpx.Response:
        if error is not None:
            raise error
        url = str(request.url)
        if "searchCities" in url:
            return httpx.Response(
                200,
                json=search_result if search_result is not None
                else load_fixture("search_namur.json"),
            )
        if "getWarnings" in url:
            return httpx.Response(200, json=load_fixture("warnings_global.json"))
        return httpx.Response(200, json=load_fixture("forecast_namur.json"))

    return handler


@pytest.fixture
def client() -> IrmApiClient:
    """Client backed by recorded fixtures, no network, no caching."""
    return IrmApiClient(transport=httpx.MockTransport(fixture_handler()), cache_ttl=0)


@pytest.fixture
def client_factory() -> Callable[..., IrmApiClient]:
    def factory(**kwargs: Any) -> IrmApiClient:
        transport = httpx.MockTransport(
            fixture_handler(
                search_result=kwargs.pop("search_result", None),
                error=kwargs.pop("error", None),
            )
        )
        return IrmApiClient(transport=transport, cache_ttl=0)

    return factory
