"""Overpass API client — fetches raw OpenStreetMap elements.

Two details here differ from the original CLI and matter:

1. **One query, not one per amenity type.** A single ``nwr`` statement with an alternation regex
   covers cafes, restaurants and fast food in one round trip. Measured against Hamburg at 12 km:
   ~10 s for ~4,000 elements, versus up to nine sequential queries before. ``nwr`` also matches
   relations, which a ``node``/``way``-only query silently dropped.

2. **The request body is raw ``text/plain``.** Overpass answers ``406 Not Acceptable`` to a
   form-encoded body sent with a custom ``User-Agent`` — and a policy-compliant User-Agent is
   not optional.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.core.errors import UpstreamError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.config import Settings
    from app.core.http import UpstreamClient
    from app.schemas.place import AmenityType

logger = logging.getLogger(__name__)

SERVICE_NAME = "Overpass API"


class OverpassService:
    """Executes Overpass QL queries and returns the decoded ``elements`` array."""

    def __init__(self, client: UpstreamClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def fetch_amenities(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_km: int,
        amenities: Sequence[AmenityType],
    ) -> list[dict[str, Any]]:
        """Return every OSM element of the given amenity types within ``radius_km``.

        Tries each configured Overpass endpoint in order (primary, then fallbacks).
        If an endpoint fails, the next one is attempted before giving up.

        Raises:
            UpstreamError: All Overpass endpoints failed or returned malformed data.
        """
        if not amenities:
            return []

        query = self.build_query(
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km,
            amenities=amenities,
        )

        overpass_urls = self._settings.overpass_urls
        last_error: UpstreamError | None = None

        for i, url in enumerate(overpass_urls, 1):
            try:
                response = await self._client.request(
                    "POST",
                    url,
                    service=SERVICE_NAME,
                    content=query,
                    # Raw body, not application/x-www-form-urlencoded — see the module docstring.
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                )
            except UpstreamError as exc:
                last_error = exc
                if i < len(overpass_urls):
                    logger.warning(
                        "Overpass endpoint %s failed, trying next mirror (%d/%d): %s",
                        url,
                        i,
                        len(overpass_urls),
                        exc.message,
                    )
                continue

            try:
                payload = response.json()
            except ValueError:
                last_error = UpstreamError(
                    SERVICE_NAME, "Overpass returned a response that was not valid JSON."
                )
                if i < len(overpass_urls):
                    logger.warning(
                        "Overpass endpoint %s returned invalid JSON, trying next mirror", url
                    )
                continue

            if not isinstance(payload, dict):
                last_error = UpstreamError(
                    SERVICE_NAME, "Overpass returned an unexpected payload shape."
                )
                continue

            if payload.get("remark"):
                last_error = UpstreamError(SERVICE_NAME, "Overpass could not complete the query.")
                continue

            elements = payload.get("elements")
            if not isinstance(elements, list) or any(
                not isinstance(item, dict) for item in elements
            ):
                last_error = UpstreamError(
                    SERVICE_NAME, "Overpass response contained no element list."
                )
                continue

            if i > 1:
                logger.info("Overpass request succeeded via fallback mirror: %s", url)

            logger.info(
                "fetched OSM elements",
                extra={
                    "element_count": len(elements),
                    "radius_km": radius_km,
                    "amenities": [amenity.value for amenity in amenities],
                },
            )
            return elements

        raise last_error or UpstreamError(SERVICE_NAME, "All Overpass endpoints failed.")

    def build_query(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_km: int,
        amenities: Sequence[AmenityType],
    ) -> str:
        """Build the Overpass QL query. Separated from I/O so it can be asserted on in tests."""
        amenity_filter = "|".join(amenity.value for amenity in amenities)
        radius_m = radius_km * 1000
        return (
            f"[out:json][timeout:{self._settings.overpass_query_timeout_seconds}];\n"
            f'nwr["amenity"~"^({amenity_filter})$"]'
            f"(around:{radius_m},{latitude},{longitude});\n"
            "out center tags;"
        )
