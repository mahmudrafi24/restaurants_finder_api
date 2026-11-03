"""Overpass client tests — query construction, the 406 workaround, and payload validation."""

from __future__ import annotations

from typing import Any

import pytest
import respx

from app.config import Settings
from app.core.errors import UpstreamError
from app.core.http import UpstreamClient
from app.schemas.place import AmenityType
from app.services.overpass import OverpassService
from tests.conftest import OVERPASS_URL


@pytest.fixture
def service(settings: Settings) -> OverpassService:
    return OverpassService(UpstreamClient(settings), settings)


class TestBuildQuery:
    def test_covers_every_amenity_in_one_statement(self, service: OverpassService) -> None:
        """One query replaces the CLI's up-to-nine sequential requests."""
        query = service.build_query(
            latitude=53.55,
            longitude=10.0,
            radius_km=12,
            amenities=list(AmenityType),
        )
        assert query.count("nwr[") == 1
        assert '"amenity"~"^(cafe|restaurant|fast_food)$"' in query

    def test_uses_nwr_to_include_relations(self, service: OverpassService) -> None:
        """A node/way-only query silently drops relation-mapped venues."""
        query = service.build_query(
            latitude=1.0, longitude=2.0, radius_km=5, amenities=[AmenityType.CAFE]
        )
        assert query.startswith("[out:json]")
        assert "nwr[" in query
        assert "node[" not in query

    def test_converts_radius_to_metres(self, service: OverpassService) -> None:
        query = service.build_query(
            latitude=1.0, longitude=2.0, radius_km=12, amenities=[AmenityType.CAFE]
        )
        assert "around:12000,1.0,2.0" in query

    def test_requests_centres_for_ways(self, service: OverpassService) -> None:
        query = service.build_query(
            latitude=1.0, longitude=2.0, radius_km=5, amenities=[AmenityType.CAFE]
        )
        assert query.rstrip().endswith("out center tags;")

    def test_includes_a_server_side_timeout(
        self, service: OverpassService, settings: Settings
    ) -> None:
        query = service.build_query(
            latitude=1.0, longitude=2.0, radius_km=5, amenities=[AmenityType.CAFE]
        )
        assert f"[timeout:{settings.overpass_query_timeout_seconds}]" in query

    def test_only_requested_amenities_appear(self, service: OverpassService) -> None:
        query = service.build_query(
            latitude=1.0, longitude=2.0, radius_km=5, amenities=[AmenityType.CAFE]
        )
        assert '"amenity"~"^(cafe)$"' in query
        assert "restaurant" not in query


class TestFetchAmenities:
    async def test_returns_the_element_list(
        self, service: OverpassService, mock_upstreams: respx.MockRouter
    ) -> None:
        mock_upstreams.post(OVERPASS_URL).respond(json={"elements": [{"type": "node", "id": 1}]})
        elements = await service.fetch_amenities(
            latitude=1.0, longitude=2.0, radius_km=5, amenities=[AmenityType.CAFE]
        )
        assert elements == [{"type": "node", "id": 1}]

    async def test_posts_a_raw_text_body(
        self, service: OverpassService, mock_upstreams: respx.MockRouter
    ) -> None:
        """Overpass answers 406 to a form-encoded body sent with a custom User-Agent, and a
        policy-compliant User-Agent is mandatory."""
        route = mock_upstreams.post(OVERPASS_URL).respond(json={"elements": []})

        await service.fetch_amenities(
            latitude=1.0, longitude=2.0, radius_km=5, amenities=[AmenityType.CAFE]
        )

        request = route.calls.last.request
        assert request.headers["content-type"].startswith("text/plain")
        body = request.content.decode()
        assert body.startswith("[out:json]")
        assert not body.startswith("data=")

    async def test_no_amenities_makes_no_request(
        self, service: OverpassService, mock_upstreams: respx.MockRouter
    ) -> None:
        route = mock_upstreams.post(OVERPASS_URL)
        assert (
            await service.fetch_amenities(latitude=1.0, longitude=2.0, radius_km=5, amenities=[])
            == []
        )
        assert not route.called

    async def test_handles_the_real_response(
        self,
        service: OverpassService,
        mock_upstreams: respx.MockRouter,
        hamburg_overpass_payload: dict[str, Any],
    ) -> None:
        mock_upstreams.post(OVERPASS_URL).respond(json=hamburg_overpass_payload)
        elements = await service.fetch_amenities(
            latitude=53.55, longitude=10.0, radius_km=12, amenities=list(AmenityType)
        )
        assert len(elements) == 3_974

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("not json at all", id="non_json_body"),
            pytest.param('["a", "list"]', id="wrong_shape"),
            pytest.param('{"remark": "runtime error"}', id="missing_elements"),
        ],
    )
    async def test_rejects_malformed_payloads(
        self, service: OverpassService, mock_upstreams: respx.MockRouter, payload: str
    ) -> None:
        mock_upstreams.post(OVERPASS_URL).respond(
            content=payload, headers={"Content-Type": "application/json"}
        )
        with pytest.raises(UpstreamError):
            await service.fetch_amenities(
                latitude=1.0, longitude=2.0, radius_km=5, amenities=[AmenityType.CAFE]
            )


async def test_slow_primary_uses_mirror_even_with_legacy_timeout_and_retry_settings(
    settings: Settings, mock_upstreams: respx.MockRouter
) -> None:
    import asyncio

    import httpx

    settings.upstream_timeout_seconds = 55
    settings.max_retries = 2
    settings.overpass_attempt_timeout_seconds = 0.02
    settings.overpass_fallback_urls = ["https://mirror.test/api/interpreter"]
    attempts = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(1)
        return httpx.Response(200, json={"elements": []})

    mock_upstreams.post(OVERPASS_URL).mock(side_effect=slow)
    mirror = mock_upstreams.post(settings.overpass_fallback_urls[0]).respond(json={"elements": []})
    client = UpstreamClient(settings)
    try:
        assert (
            await asyncio.wait_for(
                OverpassService(client, settings).fetch_amenities(
                    latitude=0, longitude=0, radius_km=1, amenities=[AmenityType.CAFE]
                ),
                0.5,
            )
            == []
        )
        assert attempts == 1
        assert mirror.call_count == 1
    finally:
        await client.aclose()


async def test_overpass_switches_mirrors_instead_of_retrying_failed_primary(
    settings: Settings, mock_upstreams: respx.MockRouter
) -> None:
    settings.max_retries = 2
    settings.overpass_fallback_urls = ["https://mirror.test/api/interpreter"]
    primary = mock_upstreams.post(OVERPASS_URL).respond(status_code=503)
    mirror = mock_upstreams.post(settings.overpass_fallback_urls[0]).respond(json={"elements": []})
    client = UpstreamClient(settings)
    try:
        assert (
            await OverpassService(client, settings).fetch_amenities(
                latitude=0, longitude=0, radius_km=1, amenities=[AmenityType.CAFE]
            )
            == []
        )
        assert primary.call_count == 1
        assert mirror.call_count == 1
    finally:
        await client.aclose()


def test_legacy_overpass_query_timeout_leaves_room_for_receiving_response(
    settings: Settings,
) -> None:
    settings.overpass_query_timeout_seconds = 50
    client = UpstreamClient(settings)
    query = OverpassService(client, settings).build_query(
        latitude=0, longitude=0, radius_km=1, amenities=[AmenityType.CAFE]
    )
    assert "[timeout:12]" in query
