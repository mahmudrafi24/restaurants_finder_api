"""Photon city suggestions: partial name → candidate places.

Photon (https://photon.komoot.io) is an OpenStreetMap geocoder purpose-built for type-ahead
search. It exists here because Nominatim cannot do the job: Nominatim matches complete names,
so a half-typed "hambu" returns a village in Papua New Guinea and never Hamburg. Nominatim
stays the authority for :meth:`~app.services.geocoding.GeocodingService.resolve`; this service
only feeds the autocomplete.

Results are cached because the same prefixes are typed over and over, and Komoot's public
instance is a free service run on donated capacity.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from app.core.cache import TTLCache
from app.core.errors import UpstreamError
from app.schemas.city import BoundingBox, City, dedupe_cities

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.http import UpstreamClient

logger = logging.getLogger(__name__)

SERVICE_NAME = "Photon"

#: Photon layer restricting results to settlements — cities down to villages. Without it the
#: index returns streets and buildings, which are not things a user picks in a city field.
_SETTLEMENT_LAYER: Final = "city"

#: Address fields, coarse to fine, used to build a readable label. Photon returns no
#: equivalent of Nominatim's ``display_name``.
_CONTEXT_FIELDS: Final[tuple[str, ...]] = ("district", "city", "county", "state", "country")


class SuggestionService:
    """Suggests cities for a partial name, via Photon."""

    def __init__(self, client: UpstreamClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._lookup_lock = asyncio.Lock()
        self._cache: TTLCache[list[City]] = TTLCache(
            ttl_seconds=settings.city_cache_ttl_seconds,
            max_entries=settings.city_cache_max_entries,
        )

    @property
    def enabled(self) -> bool:
        """Whether a Photon endpoint is configured. Callers fall back to Nominatim if not."""
        return bool(self._settings.photon_base_url.strip())

    async def suggest(self, query: str, *, limit: int | None = None) -> list[City]:
        """Coalesce concurrent cache misses within this process."""
        async with self._lookup_lock:
            return await self._lookup(query, limit=limit)

    async def _lookup(self, query: str, *, limit: int | None = None) -> list[City]:
        """Return candidate cities for a partial name, best match first.

        Returns an empty list for a blank query. Upstream failures propagate as
        :class:`~app.core.errors.UpstreamError` subclasses; the route decides whether to fall
        back or surface them.
        """
        normalized = query.strip()
        if not normalized:
            return []

        effective_limit = limit or self._settings.city_suggestion_limit
        cache_key = f"{normalized.casefold()}:{effective_limit}"

        cached = await self._cache.get(cache_key)
        if cached is not None:
            logger.debug("suggestion cache hit", extra={"query": normalized})
            return cached

        response = await self._client.request(
            "GET",
            f"{self._settings.photon_base_url.rstrip('/')}/api",
            service=SERVICE_NAME,
            params={
                "q": normalized,
                "limit": effective_limit,
                "lang": self._settings.preferred_language,
                "layer": _SETTLEMENT_LAYER,
            },
        )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(SERVICE_NAME, "Photon returned invalid JSON.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
            raise UpstreamError(SERVICE_NAME, "Photon returned an invalid feature collection.")
        features = payload["features"]
        # Photon returns one feature per OSM object, so a city mapped as both a node and a
        # boundary relation arrives twice under the same label.
        cities = dedupe_cities(
            city for raw in features if (city := _parse_feature(raw)) is not None
        )

        if features and not cities:
            raise UpstreamError(SERVICE_NAME, "Photon returned no usable records.")
        await self._cache.set(cache_key, cities)
        logger.info(
            "suggested cities",
            extra={"query": normalized, "result_count": len(cities)},
        )
        return cities


def _parse_feature(raw: object) -> City | None:
    """Skip malformed provider records without failing an entire lookup."""
    if not isinstance(raw, dict):
        return None
    try:
        return _parse_feature_record(raw)
    except (ValidationError, TypeError, ValueError, AttributeError):
        logger.warning("skipping malformed geocoder record")
        return None


def _parse_feature_record(raw: dict[str, Any]) -> City | None:
    """Convert one validated GeoJSON feature into a city."""
    geometry = raw.get("geometry") or {}
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        logger.warning("skipping Photon feature without usable coordinates")
        return None

    try:
        # GeoJSON order is [longitude, latitude].
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        logger.warning("skipping Photon feature with non-numeric coordinates")
        return None

    properties = raw.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}

    name = str(properties.get("name") or "").strip()
    if not name:
        return None

    return City(
        name=name,
        display_name=_display_name(name, properties),
        latitude=latitude,
        longitude=longitude,
        country=_text(properties.get("country")),
        country_code=_country_code(properties.get("countrycode")),
        type=_text(properties.get("osm_value")) or _text(properties.get("type")),
        bounding_box=_parse_extent(properties.get("extent")),
    )


def _display_name(name: str, properties: dict[str, Any]) -> str:
    """Compose "Hamburg, Erie County, New York, United States" from Photon's address fields.

    Duplicates are dropped: a city named the same as its county would otherwise repeat.
    """
    parts = [name]
    for field in _CONTEXT_FIELDS:
        value = _text(properties.get(field))
        if value and value not in parts:
            parts.append(value)
    return ", ".join(parts)


def _parse_extent(raw: object) -> BoundingBox | None:
    """Parse Photon's ``[west, north, east, south]`` extent.

    Note the ordering: it is neither Nominatim's ``[south, north, west, east]`` nor the GeoJSON
    bbox convention.
    """
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    try:
        west, north, east, south = (float(value) for value in raw)
    except (TypeError, ValueError):
        return None
    try:
        return BoundingBox(south=south, north=north, west=west, east=east)
    except ValidationError:
        return None


def _country_code(raw: object) -> str | None:
    """Normalise Photon's uppercase ``countrycode`` to match Nominatim's lowercase form."""
    text = _text(raw)
    return text.lower() if text else None


def _text(raw: object) -> str | None:
    """Return a stripped string, or ``None`` for missing or blank values."""
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None
