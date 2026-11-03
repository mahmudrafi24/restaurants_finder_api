"""Normalisation and deduplication, exercised against the real Hamburg Overpass response.

These tests pin the two behaviours that fix the original CLI's data loss:

* chains survive deduplication (the CLI keyed on name and lost 527 of 3,951 rows);
* relations are collected, not just nodes and ways.
"""

from __future__ import annotations

from typing import Any

from app.schemas.place import AmenityType
from app.services.places import build_stats, normalize_elements


class TestNormalizeElements:
    def test_collects_all_named_places_from_real_response(
        self, hamburg_elements: list[dict[str, Any]]
    ) -> None:
        places = normalize_elements(hamburg_elements)

        named = [
            element
            for element in hamburg_elements
            if (element.get("tags") or {}).get("name", "").strip()
        ]
        # 3,951 named of 3,974 elements. Allow for the double-mapping collapse, but the result
        # must stay far above the 3,424 that name-based dedup would leave.
        assert len(places) > 3_500
        assert len(places) <= len(named)

    def test_keeps_every_branch_of_a_chain(self, hamburg_elements: list[dict[str, Any]]) -> None:
        """The CLI's `seen.add(name)` collapsed chains to one row; ids must not."""
        places = normalize_elements(hamburg_elements)

        counts: dict[str, int] = {}
        for place in places:
            counts[place.name.casefold()] = counts.get(place.name.casefold(), 0) + 1

        repeated = {name: count for name, count in counts.items() if count > 1}
        assert repeated, "expected chains such as McDonald's to appear multiple times"
        assert max(repeated.values()) > 5

    def test_osm_ids_are_unique(self, hamburg_elements: list[dict[str, Any]]) -> None:
        places = normalize_elements(hamburg_elements)
        ids = [place.osm_id for place in places]
        assert len(ids) == len(set(ids))

    def test_includes_way_backed_places(self, hamburg_elements: list[dict[str, Any]]) -> None:
        """Ways carry coordinates in `center`, not `lat`/`lon`."""
        places = normalize_elements(hamburg_elements)
        assert any(place.osm_id.startswith("way/") for place in places)

    def test_every_place_has_valid_coordinates(
        self, hamburg_elements: list[dict[str, Any]]
    ) -> None:
        for place in normalize_elements(hamburg_elements):
            assert -90 <= place.latitude <= 90
            assert -180 <= place.longitude <= 180

    def test_results_are_sorted_by_name(self, hamburg_elements: list[dict[str, Any]]) -> None:
        places = normalize_elements(hamburg_elements)
        names = [place.name.casefold() for place in places]
        assert names == sorted(names)

    def test_skips_unnamed_elements(self) -> None:
        elements = [
            {"type": "node", "id": 1, "lat": 1.0, "lon": 2.0, "tags": {"amenity": "cafe"}},
            {
                "type": "node",
                "id": 2,
                "lat": 1.0,
                "lon": 2.0,
                "tags": {"amenity": "cafe", "name": "   "},
            },
        ]
        assert normalize_elements(elements) == []

    def test_skips_elements_without_coordinates(self) -> None:
        elements = [{"type": "way", "id": 1, "tags": {"amenity": "cafe", "name": "Nowhere"}}]
        assert normalize_elements(elements) == []

    def test_skips_unsupported_amenity(self) -> None:
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 1.0,
                "lon": 2.0,
                "tags": {"amenity": "pharmacy", "name": "Not Food"},
            }
        ]
        assert normalize_elements(elements) == []

    def test_splits_cuisine_on_semicolons(self) -> None:
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 1.0,
                "lon": 2.0,
                "tags": {"amenity": "restaurant", "name": "Fusion", "cuisine": "thai;italian;"},
            }
        ]
        (place,) = normalize_elements(elements)
        assert place.cuisine == ["thai", "italian"]

    def test_builds_formatted_address(self) -> None:
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 1.0,
                "lon": 2.0,
                "tags": {
                    "amenity": "cafe",
                    "name": "Corner Cafe",
                    "addr:housenumber": "12",
                    "addr:street": "Main Street",
                    "addr:city": "Hamburg",
                    "addr:postcode": "20095",
                },
            }
        ]
        (place,) = normalize_elements(elements)
        assert place.address.formatted == "12, Main Street, Hamburg, 20095"

    def test_address_is_none_when_untagged(self) -> None:
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 1.0,
                "lon": 2.0,
                "tags": {"amenity": "cafe", "name": "Bare"},
            }
        ]
        (place,) = normalize_elements(elements)
        assert place.address.formatted is None

    def test_falls_back_to_contact_prefixed_tags(self) -> None:
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 1.0,
                "lon": 2.0,
                "tags": {
                    "amenity": "cafe",
                    "name": "Contact Cafe",
                    "contact:phone": "+49 40 111",
                    "contact:website": "https://example.com",
                },
            }
        ]
        (place,) = normalize_elements(elements)
        assert place.phone == "+49 40 111"
        assert place.website == "https://example.com"

    def test_takes_first_value_from_semicolon_list(self) -> None:
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 1.0,
                "lon": 2.0,
                "tags": {"amenity": "cafe", "name": "Two Phones", "phone": "+49 1;+49 2"},
            }
        ]
        (place,) = normalize_elements(elements)
        assert place.phone == "+49 1"

    def test_adds_missing_url_scheme(self) -> None:
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 1.0,
                "lon": 2.0,
                "tags": {"amenity": "cafe", "name": "Schemeless", "website": "www.example.com"},
            }
        ]
        (place,) = normalize_elements(elements)
        assert place.website == "https://www.example.com"

    def test_drops_website_that_is_not_a_url(self) -> None:
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 1.0,
                "lon": 2.0,
                "tags": {"amenity": "cafe", "name": "Bad URL", "website": "ask inside"},
            }
        ]
        (place,) = normalize_elements(elements)
        assert place.website is None

    def test_builds_maps_url_from_coordinates(self) -> None:
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 53.5,
                "lon": 10.0,
                "tags": {"amenity": "cafe", "name": "Mapped"},
            }
        ]
        (place,) = normalize_elements(elements)
        assert place.maps_url.endswith("query=53.5,10.0")


class TestDoubleMappingCollapse:
    """A venue mapped as both a node and a building way should yield one row."""

    @staticmethod
    def _pair() -> list[dict[str, Any]]:
        return [
            {
                "type": "node",
                "id": 1,
                "lat": 53.5503,
                "lon": 9.9937,
                "tags": {"amenity": "cafe", "name": "Cafe Twice", "phone": "+49 40 999"},
            },
            {
                "type": "way",
                "id": 2,
                "center": {"lat": 53.55031, "lon": 9.99371},
                "tags": {
                    "amenity": "cafe",
                    "name": "Cafe Twice",
                    "phone": "+49 40 999",
                    "website": "https://twice.example",
                },
            },
        ]

    def test_collapses_to_one_row(self) -> None:
        assert len(normalize_elements(self._pair())) == 1

    def test_keeps_the_richer_record(self) -> None:
        (place,) = normalize_elements(self._pair())
        assert place.phone == "+49 40 999"
        assert place.website == "https://twice.example"

    def test_keeps_same_named_venues_that_are_far_apart(self) -> None:
        """Two branches of one chain in different neighbourhoods are distinct places."""
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 53.5503,
                "lon": 9.9937,
                "tags": {"amenity": "fast_food", "name": "Burger Chain"},
            },
            {
                "type": "node",
                "id": 2,
                "lat": 53.6100,
                "lon": 10.0500,
                "tags": {"amenity": "fast_food", "name": "Burger Chain"},
            },
        ]
        assert len(normalize_elements(elements)) == 2

    def test_keeps_different_amenities_at_one_address(self) -> None:
        """A cafe and a restaurant sharing a name and a building are separate listings."""
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 53.5503,
                "lon": 9.9937,
                "tags": {"amenity": "cafe", "name": "Hotel Vier"},
            },
            {
                "type": "node",
                "id": 2,
                "lat": 53.5503,
                "lon": 9.9937,
                "tags": {"amenity": "restaurant", "name": "Hotel Vier"},
            },
        ]
        assert len(normalize_elements(elements)) == 2


class TestBuildStats:
    def test_counts_by_amenity_and_completeness(self) -> None:
        places = normalize_elements(
            [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 1.0,
                    "lon": 1.0,
                    "tags": {"amenity": "cafe", "name": "A", "phone": "1"},
                },
                {
                    "type": "node",
                    "id": 2,
                    "lat": 2.0,
                    "lon": 2.0,
                    "tags": {
                        "amenity": "restaurant",
                        "name": "B",
                        "website": "https://b.example",
                        "opening_hours": "Mo-Fr 09:00-17:00",
                    },
                },
            ]
        )
        stats = build_stats(places)

        assert stats.total == 2
        assert stats.by_amenity == {"cafe": 1, "restaurant": 1}
        assert stats.with_phone == 1
        assert stats.with_website == 1
        assert stats.with_opening_hours == 1

    def test_real_response_amenity_mix(self, hamburg_elements: list[dict[str, Any]]) -> None:
        stats = build_stats(normalize_elements(hamburg_elements))
        assert set(stats.by_amenity) == {amenity.value for amenity in AmenityType}
        assert stats.total == sum(stats.by_amenity.values())
