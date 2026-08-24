"""Shared fixtures.

Upstream calls are mocked with ``respx`` throughout, so the suite is deterministic and never
touches Nominatim or Overpass. The Overpass fixture is a real captured response (Hamburg, 12 km,
3,974 elements) so normalisation is exercised against genuine OSM data rather than hand-written
samples.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app

FIXTURE_DIR = Path(__file__).parent / "fixtures"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PHOTON_URL = "https://photon.komoot.io/api"


@pytest.fixture(scope="session")
def hamburg_overpass_payload() -> dict[str, Any]:
    """Real Overpass response: Hamburg, 12 km radius, all three amenity types."""
    with (FIXTURE_DIR / "overpass_hamburg_12km.json").open(encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


@pytest.fixture(scope="session")
def hamburg_elements(hamburg_overpass_payload: dict[str, Any]) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = hamburg_overpass_payload["elements"]
    return elements


@pytest.fixture
def photon_hamburg() -> dict[str, Any]:
    """A real Photon ``/api`` response for the partial query "hambu".

    Captured live. It is the case Nominatim cannot serve: a prefix that only matches Hamburg
    once an autocomplete-capable index is doing the matching.
    """
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "osm_type": "R",
                    "osm_id": 62782,
                    "osm_key": "place",
                    "osm_value": "city",
                    "type": "city",
                    "name": "Hamburg",
                    "country": "Germany",
                    "countrycode": "DE",
                    "extent": [8.1044993, 54.02765, 10.3252805, 53.3951118],
                },
                "geometry": {"type": "Point", "coordinates": [10.0013165, 53.5501721]},
            },
            {
                "type": "Feature",
                "properties": {
                    "osm_value": "town",
                    "type": "city",
                    "name": "Hamburg",
                    "county": "Erie County",
                    "state": "New York",
                    "country": "United States",
                    "countrycode": "US",
                    "extent": [-78.9703478, 42.8058587, -78.784354, 42.696215],
                },
                "geometry": {"type": "Point", "coordinates": [-78.828717, 42.716293]},
            },
        ],
    }


@pytest.fixture
def nominatim_hamburg() -> list[dict[str, Any]]:
    """A single Nominatim result shaped like the real ``jsonv2`` payload."""
    return [
        {
            "place_id": 12345,
            "lat": "53.550341",
            "lon": "10.000654",
            "name": "Hamburg",
            "display_name": "Hamburg, Germany",
            "addresstype": "city",
            "type": "city",
            "country_code": "de",
            "boundingbox": ["53.3949", "53.9644", "8.1053", "10.3253"],
            "address": {"country": "Germany", "country_code": "de"},
        }
    ]


@pytest.fixture
def settings() -> Settings:
    """Test settings with retries and caching disabled for predictable behaviour.

    The contact address deliberately avoids a reserved domain: those are stripped from the
    User-Agent, because Nominatim rejects them with HTTP 403.
    """
    return Settings(
        environment="development",
        contact_email="tests@acme.dev",
        max_retries=0,
        retry_backoff_seconds=0.0,
        city_cache_ttl_seconds=0,  # disables the geocoding cache
        upstream_timeout_seconds=5.0,
        overpass_fallback_urls=[],  # tests mock only the primary URL
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def mock_upstreams() -> Iterator[respx.MockRouter]:
    """Intercept all outbound HTTP; any unmocked call fails the test."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI test client that runs the application's lifespan."""
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
        # Entering the lifespan populates app.state with the services the routes depend on.
        _lifespan(app),
    ):
        yield http_client


class _lifespan:
    """Minimal async context manager driving FastAPI's lifespan in tests."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._context: Any = None

    async def __aenter__(self) -> None:
        self._context = self._app.router.lifespan_context(self._app)
        await self._context.__aenter__()

    async def __aexit__(self, *exc_info: object) -> None:
        await self._context.__aexit__(*exc_info)
