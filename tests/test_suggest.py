"""Photon-backed city suggestions."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from app.config import USER_AGENT_PRODUCT, Settings
from app.core.errors import UpstreamError, UpstreamUnavailableError
from app.core.http import UpstreamClient
from app.services.suggest import SuggestionService
from tests.conftest import PHOTON_URL


@pytest.fixture
def service(settings: Settings) -> SuggestionService:
    return SuggestionService(UpstreamClient(settings), settings)


class TestSuggest:
    async def test_returns_cities_for_a_partial_name(
        self,
        service: SuggestionService,
        mock_upstreams: respx.MockRouter,
        photon_hamburg: dict[str, Any],
    ) -> None:
        """The whole reason this service exists: "hambu" must find Hamburg."""
        mock_upstreams.get(PHOTON_URL).respond(json=photon_hamburg)

        results = await service.suggest("hambu")

        assert [city.name for city in results] == ["Hamburg", "Hamburg"]
        assert results[0].country == "Germany"

    async def test_reads_geojson_coordinate_order(
        self,
        service: SuggestionService,
        mock_upstreams: respx.MockRouter,
        photon_hamburg: dict[str, Any],
    ) -> None:
        """GeoJSON is [longitude, latitude]; swapping them puts Hamburg in the ocean."""
        mock_upstreams.get(PHOTON_URL).respond(json=photon_hamburg)

        hamburg = (await service.suggest("hambu"))[0]

        assert hamburg.latitude == pytest.approx(53.5501721)
        assert hamburg.longitude == pytest.approx(10.0013165)

    async def test_parses_the_extent_ordering(
        self,
        service: SuggestionService,
        mock_upstreams: respx.MockRouter,
        photon_hamburg: dict[str, Any],
    ) -> None:
        """Photon's extent is [west, north, east, south] — its own ordering, not GeoJSON's."""
        mock_upstreams.get(PHOTON_URL).respond(json=photon_hamburg)

        box = (await service.suggest("hambu"))[0].bounding_box

        assert box is not None
        assert box.west == pytest.approx(8.1044993)
        assert box.north == pytest.approx(54.02765)
        assert box.east == pytest.approx(10.3252805)
        assert box.south == pytest.approx(53.3951118)
        assert box.south < box.north and box.west < box.east

    async def test_builds_a_distinguishing_label(
        self,
        service: SuggestionService,
        mock_upstreams: respx.MockRouter,
        photon_hamburg: dict[str, Any],
    ) -> None:
        """Photon has no display_name, and two entries here are both called "Hamburg"."""
        mock_upstreams.get(PHOTON_URL).respond(json=photon_hamburg)

        labels = [city.display_name for city in await service.suggest("hambu")]

        assert labels == [
            "Hamburg, Germany",
            "Hamburg, Erie County, New York, United States",
        ]

    async def test_label_does_not_repeat_a_name(
        self, service: SuggestionService, mock_upstreams: respx.MockRouter
    ) -> None:
        mock_upstreams.get(PHOTON_URL).respond(
            json={
                "features": [
                    {
                        "properties": {
                            "name": "Singapore",
                            "city": "Singapore",
                            "country": "Singapore",
                        },
                        "geometry": {"coordinates": [103.85, 1.29]},
                    }
                ]
            }
        )

        assert (await service.suggest("singa"))[0].display_name == "Singapore"

    async def test_restricts_results_to_settlements(
        self,
        service: SuggestionService,
        mock_upstreams: respx.MockRouter,
        photon_hamburg: dict[str, Any],
    ) -> None:
        """Without the layer filter the index also returns streets and buildings."""
        route = mock_upstreams.get(PHOTON_URL).respond(json=photon_hamburg)

        await service.suggest("hambu")

        assert route.calls.last.request.url.params["layer"] == "city"

    async def test_requests_labels_in_the_configured_language(
        self,
        service: SuggestionService,
        mock_upstreams: respx.MockRouter,
        photon_hamburg: dict[str, Any],
    ) -> None:
        route = mock_upstreams.get(PHOTON_URL).respond(json=photon_hamburg)

        await service.suggest("hambu")

        assert route.calls.last.request.url.params["lang"] == "en"

    async def test_normalises_the_country_code(
        self,
        service: SuggestionService,
        mock_upstreams: respx.MockRouter,
        photon_hamburg: dict[str, Any],
    ) -> None:
        """Photon uppercases it, Nominatim lowercases it; clients should see one form."""
        mock_upstreams.get(PHOTON_URL).respond(json=photon_hamburg)
        assert (await service.suggest("hambu"))[0].country_code == "de"

    async def test_honours_the_limit(
        self,
        service: SuggestionService,
        mock_upstreams: respx.MockRouter,
        photon_hamburg: dict[str, Any],
    ) -> None:
        route = mock_upstreams.get(PHOTON_URL).respond(json=photon_hamburg)

        await service.suggest("hambu", limit=3)

        assert route.calls.last.request.url.params["limit"] == "3"

    async def test_blank_query_makes_no_request(
        self, service: SuggestionService, mock_upstreams: respx.MockRouter
    ) -> None:
        route = mock_upstreams.get(PHOTON_URL)
        assert await service.suggest("   ") == []
        assert not route.calls

    async def test_sends_a_policy_compliant_user_agent(
        self,
        service: SuggestionService,
        mock_upstreams: respx.MockRouter,
        photon_hamburg: dict[str, Any],
    ) -> None:
        route = mock_upstreams.get(PHOTON_URL).respond(json=photon_hamburg)

        await service.suggest("hambu")

        assert USER_AGENT_PRODUCT in route.calls.last.request.headers["user-agent"]

    async def test_upstream_failure_propagates(
        self, service: SuggestionService, mock_upstreams: respx.MockRouter
    ) -> None:
        """The route decides whether to fall back; the service does not swallow errors."""
        mock_upstreams.get(PHOTON_URL).mock(side_effect=httpx.ConnectError("down"))

        with pytest.raises(UpstreamUnavailableError):
            await service.suggest("hambu")


class TestMalformedResponses:
    @pytest.mark.parametrize(
        ("payload", "reason"),
        [
            ({}, "no features key"),
            ({"features": []}, "no matches"),
            ({"features": [{"properties": {"name": "X"}}]}, "no geometry"),
            (
                {"features": [{"properties": {"name": "X"}, "geometry": {"coordinates": [1]}}]},
                "incomplete coordinate pair",
            ),
            (
                {
                    "features": [
                        {"properties": {"name": "X"}, "geometry": {"coordinates": ["a", "b"]}}
                    ]
                },
                "non-numeric coordinates",
            ),
            (
                {"features": [{"properties": {}, "geometry": {"coordinates": [1.0, 2.0]}}]},
                "unnamed place is not selectable",
            ),
            ({"features": ["nonsense"]}, "feature is not an object"),
        ],
    )
    async def test_unusable_payloads_trigger_fallback_except_valid_empty_results(
        self,
        service: SuggestionService,
        mock_upstreams: respx.MockRouter,
        payload: dict[str, Any],
        reason: str,
    ) -> None:
        """A completely unusable response triggers the route's provider fallback."""
        mock_upstreams.get(PHOTON_URL).respond(json=payload)
        if payload == {"features": []}:
            assert await service.suggest("query") == [], reason
        else:
            with pytest.raises(UpstreamError):
                await service.suggest("query")


class TestDeduplication:
    """OSM maps one settlement as several objects; Photon returns one feature per object."""

    @staticmethod
    def _feature(name: str, longitude: float, latitude: float) -> dict[str, Any]:
        return {
            "properties": {"name": name, "state": "South Holland", "country": "Netherlands"},
            "geometry": {"coordinates": [longitude, latitude]},
        }

    async def test_the_same_place_twice_collapses_to_one_entry(
        self, service: SuggestionService, mock_upstreams: respx.MockRouter
    ) -> None:
        """Leiden really is returned twice by the live API — node and boundary relation."""
        mock_upstreams.get(PHOTON_URL).respond(
            json={
                "features": [
                    self._feature("Leiden", 4.4908843, 52.1594747),
                    self._feature("Leiden", 4.4908843, 52.1594747),
                    self._feature("Leidschendam", 4.4000000, 52.0900000),
                ]
            }
        )

        results = await service.suggest("leid")

        assert [city.name for city in results] == ["Leiden", "Leidschendam"]

    async def test_distinct_places_sharing_a_label_are_both_kept(
        self, service: SuggestionService, mock_upstreams: respx.MockRouter
    ) -> None:
        """Deduping on the label alone would silently hide one of two real settlements."""
        mock_upstreams.get(PHOTON_URL).respond(
            json={
                "features": [
                    self._feature("Rijswijk", 4.3200000, 52.0360000),
                    self._feature("Rijswijk", 5.0400000, 51.8400000),
                ]
            }
        )

        assert len(await service.suggest("rijs")) == 2

    async def test_near_identical_coordinates_are_treated_as_one_place(
        self, service: SuggestionService, mock_upstreams: respx.MockRouter
    ) -> None:
        """Two records of one city rarely agree to the last decimal; 11 m apart is one city."""
        mock_upstreams.get(PHOTON_URL).respond(
            json={
                "features": [
                    self._feature("Delft", 4.3570000, 52.0116000),
                    self._feature("Delft", 4.3570001, 52.0116002),
                ]
            }
        )

        assert len(await service.suggest("delf")) == 1


class TestCaching:
    async def test_repeated_prefixes_hit_the_cache(
        self,
        settings: Settings,
        mock_upstreams: respx.MockRouter,
        photon_hamburg: dict[str, Any],
    ) -> None:
        """Type-ahead resends the same prefixes constantly, and Komoot's public instance runs
        on donated capacity."""
        cached_settings = settings.model_copy(update={"city_cache_ttl_seconds": 600})
        service = SuggestionService(UpstreamClient(cached_settings), cached_settings)
        route = mock_upstreams.get(PHOTON_URL).respond(json=photon_hamburg)

        await service.suggest("hambu")
        await service.suggest("HAMBU")  # case-insensitive key

        assert route.call_count == 1


class TestEnabled:
    def test_enabled_when_configured(self, service: SuggestionService) -> None:
        assert service.enabled

    def test_disabled_when_base_url_is_blank(self, settings: Settings) -> None:
        """Blanking the URL is the documented way to opt out and fall back to Nominatim."""
        without_photon = settings.model_copy(update={"photon_base_url": "  "})
        assert not SuggestionService(UpstreamClient(without_photon), without_photon).enabled
