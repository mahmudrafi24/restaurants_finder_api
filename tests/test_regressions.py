"""Regression cases from the November bug audit: unsafe data and failure paths."""

from __future__ import annotations

import asyncio
import csv
import io
from typing import Any

import httpx
import pytest
import respx
from openpyxl import load_workbook
from pydantic import ValidationError

from app.config import Settings
from app.core.errors import UpstreamError
from app.core.http import UpstreamClient
from app.main import create_app
from app.schemas.place import MAX_REQUEST_BYTES, AmenityType, Place, SearchRequest
from app.services.exporters.csv import CsvExporter
from app.services.exporters.xlsx import XlsxExporter
from app.services.geocoding import GeocodingService, _parse_city
from app.services.overpass import OverpassService
from app.services.places import _normalize_website, normalize_elements
from app.services.suggest import _parse_feature
from tests.conftest import NOMINATIM_URL, OVERPASS_URL, PHOTON_URL, _lifespan


def venue(name: str) -> Place:
    return Place(osm_id="node/1", name=name, amenity="cafe", latitude=0, longitude=0)


@pytest.mark.parametrize(
    "text", ["=1+1", "+1+1", "-1+1", "@SUM(1,2)", " \t=1+1", "\r=1+1", "\u00a0=1+1"]
)
def test_spreadsheets_keep_external_text_literal(text: str) -> None:
    place = venue(text)
    sheet = load_workbook(io.BytesIO(XlsxExporter().render([place], city_name="Test"))).active
    assert sheet is not None
    assert sheet["A3"].data_type == "s"
    assert sheet["A3"].value == text.replace("\r", "\n")
    rows = list(
        csv.reader(io.StringIO(CsvExporter().render([place], city_name="Test").decode("utf-8-sig")))
    )
    assert rows[1][0] == "'" + text
    assert sheet["H3"].data_type == "n"  # coordinates remain numeric


def test_workbook_removes_illegal_xml_from_title_and_cells() -> None:
    content = XlsxExporter().render([venue("Bad\x01Name")], city_name="City\x00")
    sheet = load_workbook(io.BytesIO(content)).active
    assert sheet is not None
    assert sheet["A3"].value == "BadName"
    assert "City" in sheet["A1"].value


@pytest.mark.parametrize("value", [" a ", "  ", "a"])
def test_trim_before_city_length(value: str) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(city=value)


def test_export_fields_have_bounds() -> None:
    with pytest.raises(ValidationError):
        venue("x" * 2001)


@pytest.mark.parametrize(
    "value",
    [
        "https://",
        "http://bad host",
        "ftp://example.org",
        "javascript:alert(1)",
        "https://user:pw@example.org",
        "https://bad..org",
        "https://example.org:99999",
    ],
)
def test_invalid_websites_are_dropped(value: str) -> None:
    assert _normalize_website(value) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("www.example.org", "https://www.example.org"),
        ("https://example.org/menu?q=1", "https://example.org/menu?q=1"),
    ],
)
def test_valid_websites(value: str, expected: str) -> None:
    assert _normalize_website(value) == expected


def test_dedup_preserves_nearby_nodes_and_uncorroborated_outlines() -> None:
    tags = {"name": "Chain", "amenity": "cafe"}
    elements = [
        {"type": "node", "id": 1, "lat": 0, "lon": 0, "tags": tags},
        {"type": "node", "id": 2, "lat": 0.00001, "lon": 0, "tags": tags},
        {"type": "way", "id": 3, "center": {"lat": 0, "lon": 0}, "tags": tags},
    ]
    assert len(normalize_elements(elements)) == 3


def test_dedup_across_rounding_boundary_merges_complementary_data() -> None:
    tags = {"name": "Cafe", "amenity": "cafe", "addr:street": "Main", "addr:housenumber": "1"}
    places = normalize_elements(
        [
            {"type": "node", "id": 1, "lat": 0.000049, "lon": 0, "tags": {**tags, "phone": "123"}},
            {
                "type": "way",
                "id": 2,
                "center": {"lat": 0.000051, "lon": 0},
                "tags": {**tags, "website": "https://example.org"},
            },
        ]
    )
    assert len(places) == 1
    assert places[0].phone == "123"
    assert places[0].website == "https://example.org"


def test_ambiguous_double_mappings_are_preserved() -> None:
    tags = {"name": "Chain", "amenity": "cafe", "phone": "123"}
    places = normalize_elements(
        [
            {"type": "node", "id": 1, "lat": 0, "lon": 0, "tags": tags},
            {"type": "node", "id": 2, "lat": 0.00001, "lon": 0, "tags": tags},
            {"type": "way", "id": 3, "center": {"lat": 0, "lon": 0}, "tags": tags},
        ]
    )
    assert len(places) == 3


@pytest.mark.parametrize(
    "raw", [None, "oops", {"lat": "999", "lon": "0"}, {"lat": "nan", "lon": "0"}]
)
def test_unusable_geocoder_records_do_not_raise(raw: Any) -> None:
    assert _parse_city(raw) is None
    assert _parse_feature(raw) is None


@pytest.mark.parametrize("coordinate", [10**400, -(10**400)])
def test_overflowing_provider_coordinates_are_skipped(coordinate: int) -> None:
    for latitude, longitude in ((coordinate, 0), (0, coordinate)):
        assert _parse_city({"lat": latitude, "lon": longitude}) is None
        assert (
            _parse_feature(
                {
                    "geometry": {"coordinates": [longitude, latitude]},
                    "properties": {"name": "Test"},
                }
            )
            is None
        )
        for kind in ("node", "way", "relation"):
            assert (
                normalize_elements(
                    [
                        {
                            "type": kind,
                            "id": 1,
                            "lat": latitude,
                            "lon": longitude,
                            "center": {"lat": latitude, "lon": longitude},
                            "tags": {"name": "Test", "amenity": "cafe"},
                        }
                    ]
                )
                == []
            )


@pytest.mark.parametrize("index", range(4))
def test_overflowing_optional_bounds_preserve_valid_cities(index: int) -> None:
    bounds = [0, 0, 0, 0]
    bounds[index] = 10**400
    nominatim = _parse_city({"lat": 0, "lon": 0, "name": "Test", "boundingbox": bounds})
    photon = _parse_feature(
        {
            "geometry": {"coordinates": [0, 0]},
            "properties": {"name": "Test", "extent": bounds},
        }
    )
    for city in (nominatim, photon):
        assert city is not None
        assert city.name == "Test"
        assert city.bounding_box is None


@pytest.mark.parametrize(
    "payload",
    [
        {"features": None},
        [1],
        {"features": [{"geometry": {"coordinates": [0, 10**400]}, "properties": {"name": "Bad"}}]},
        {"features": [{"geometry": {"coordinates": [0, 999]}, "properties": {"name": "Bad"}}]},
    ],
)
async def test_malformed_photon_activates_route_fallback(
    client: httpx.AsyncClient,
    mock_upstreams: respx.MockRouter,
    nominatim_hamburg: list[dict[str, Any]],
    payload: Any,
) -> None:
    mock_upstreams.get(PHOTON_URL).respond(json=payload)
    route = mock_upstreams.get(NOMINATIM_URL).respond(json=nominatim_hamburg)
    response = await client.get("/api/v1/cities/search", params={"q": "Hamburg"})
    assert response.status_code == 200
    assert response.json()["results"][0]["name"] == "Hamburg"
    assert route.call_count == 1


async def test_partial_overpass_response_uses_fallback(
    settings: Settings, mock_upstreams: respx.MockRouter
) -> None:
    settings.overpass_fallback_urls = ["https://mirror.test/api/interpreter"]
    mock_upstreams.post(OVERPASS_URL).respond(
        json={"elements": [], "remark": "runtime error: timeout"}
    )
    mirror = mock_upstreams.post(settings.overpass_fallback_urls[0]).respond(
        json={"elements": [{"type": "node", "id": 1}]}
    )
    upstream = UpstreamClient(settings)
    try:
        result = await OverpassService(upstream, settings).fetch_amenities(
            latitude=0, longitude=0, radius_km=1, amenities=[AmenityType.CAFE]
        )
        assert len(result) == 1
        assert mirror.call_count == 1
    finally:
        await upstream.aclose()


async def test_nominatim_invalid_json_is_controlled(
    settings: Settings, mock_upstreams: respx.MockRouter
) -> None:
    mock_upstreams.get(NOMINATIM_URL).respond(content=b"<html>bad</html>")
    upstream = UpstreamClient(settings)
    try:
        with pytest.raises(UpstreamError):
            await GeocodingService(upstream, settings).search("Hamburg")
    finally:
        await upstream.aclose()


async def test_total_deadline_covers_slow_provider(
    settings: Settings, mock_upstreams: respx.MockRouter
) -> None:
    settings.request_timeout_seconds = 0.03
    settings.max_retries = 2
    app = create_app(settings)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={"elements": []})

    mock_upstreams.post(OVERPASS_URL).mock(side_effect=slow)
    async with (
        _lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/api/v1/places/search", json={"city": "Test", "latitude": 0, "longitude": 0}
        )
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"


async def test_oversized_utf8_request_is_rejected_before_json_parse(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/v1/places/export", content=b"x" * (MAX_REQUEST_BYTES + 1))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


async def test_request_rate_limit_does_not_block_health(
    settings: Settings, mock_upstreams: respx.MockRouter
) -> None:
    settings.requests_per_minute = 1
    mock_upstreams.get(PHOTON_URL).respond(json={"features": []})
    app = create_app(settings)
    async with (
        _lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/api/v1/cities/search?q=ab")).status_code == 200
        assert (await client.get("/api/v1/cities/search?q=ab")).status_code == 429
        assert (await client.get("/api/v1/health")).status_code == 200


async def test_health_uses_factory_settings(settings: Settings) -> None:
    settings.environment = "staging"
    app = create_app(settings)
    async with (
        _lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/api/v1/health")).json()["environment"] == "staging"


async def test_concurrent_suggestion_misses_share_one_lookup(
    settings: Settings, mock_upstreams: respx.MockRouter, photon_hamburg: dict[str, Any]
) -> None:
    from app.services.suggest import SuggestionService

    settings.city_cache_ttl_seconds = 3600

    async def reply(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=photon_hamburg)

    route = mock_upstreams.get(PHOTON_URL).mock(side_effect=reply)
    upstream = UpstreamClient(settings)
    try:
        service = SuggestionService(upstream, settings)
        results = await asyncio.gather(*(service.suggest("Hamburg") for _ in range(3)))
        assert all(result[0].name == "Hamburg" for result in results)
        assert route.call_count == 1
    finally:
        await upstream.aclose()


async def test_concurrent_search_capacity_does_not_block_health(
    settings: Settings, mock_upstreams: respx.MockRouter
) -> None:
    settings.max_concurrent_requests = 1
    app = create_app(settings)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        await finish.wait()
        return httpx.Response(200, json={"elements": []})

    mock_upstreams.post(OVERPASS_URL).mock(side_effect=slow)
    async with (
        _lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        pending = asyncio.create_task(
            client.post(
                "/api/v1/places/search", json={"city": "Test", "latitude": 0, "longitude": 0}
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        try:
            assert (await client.get("/api/v1/cities/search?q=ab")).status_code == 503
            assert (await client.get("/api/v1/health")).status_code == 200
        finally:
            finish.set()
            assert (await pending).status_code == 200


async def test_slow_export_keeps_health_responsive(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from app.api.v1.routes import places

    entered, finish = threading.Event(), threading.Event()

    class SlowExporter:
        extension = "csv"
        media_type = "text/csv"

        def render(self, rows: Any, *, city_name: str) -> bytes:
            entered.set()
            assert finish.wait(2)
            return b"Name\r\nCafe\r\n"

    monkeypatch.setattr(places, "get_exporter", lambda _: SlowExporter())
    app = create_app(settings)
    async with (
        _lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        pending = asyncio.create_task(
            client.post(
                "/api/v1/places/export",
                json={"city_name": "Test", "format": "csv", "places": [venue("Cafe").model_dump()]},
            )
        )
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
        try:
            response = await asyncio.wait_for(client.get("/api/v1/health"), 0.5)
            assert response.status_code == 200
            assert not pending.done()
        finally:
            finish.set()
            assert (await pending).status_code == 200


async def test_padded_short_city_query_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/cities/search", params={"q": " a "})
    assert response.status_code == 422
