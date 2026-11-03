"""Place search and file export."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, Request, Response, status

from app.api.deps import PlaceSearchDep
from app.schemas.place import ExportRequest, SearchRequest, SearchResponse
from app.services.exporters.base import build_filename, get_exporter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/places", tags=["places"])


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Find cafes, restaurants and fast food near a city",
    responses={
        404: {"description": "The city name could not be geocoded."},
        429: {"description": "An upstream provider is rate limiting requests."},
        504: {"description": "An upstream provider timed out; try a smaller radius."},
    },
)
async def search_places(payload: SearchRequest, service: PlaceSearchDep) -> SearchResponse:
    """Geocode the city, then collect every matching amenity within the radius.

    A dense metropolitan area at a large radius can return several thousand rows and take around
    ten seconds — the response is complete rather than paginated, so the client can sort, filter
    and export without further round trips.

    Passing `latitude`/`longitude` from a chosen suggestion skips the geocoding step.
    """
    return await service.search(
        city=payload.city,
        radius_km=payload.radius_km,
        amenities=payload.amenities,
        coordinates=payload.coordinates,
    )


@router.post(
    "/export",
    summary="Download search results as XLSX, CSV or GeoJSON",
    response_class=Response,
    responses={
        200: {
            "description": "The rendered file.",
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {},
                "text/csv": {},
                "application/geo+json": {},
            },
        },
        413: {"description": "Too many rows submitted."},
    },
    status_code=status.HTTP_200_OK,
)
async def export_places(payload: ExportRequest, request: Request) -> Response:
    """Render already-fetched rows into a downloadable file.

    Stateless by design: the client posts back the rows it received from ``/places/search``, so
    Overpass is never queried twice for data the caller already holds.
    """
    exporter = get_exporter(payload.format)
    # Keep slots occupied until rendering actually ends, even if the caller times out.
    slots: asyncio.Semaphore = request.app.state.export_slots
    await slots.acquire()
    task = asyncio.create_task(
        asyncio.to_thread(exporter.render, payload.places, city_name=payload.city_name)
    )

    def rendered(completed: asyncio.Task[bytes]) -> None:
        slots.release()
        if not completed.cancelled():
            completed.exception()  # retrieve an exception even if the request was cancelled

    task.add_done_callback(rendered)
    content = await asyncio.shield(task)
    filename = build_filename(payload.city_name, exporter.extension)

    logger.info(
        "rendered export",
        extra={
            "format": payload.format.value,
            "row_count": len(payload.places),
            "bytes": len(content),
        },
    )

    return Response(
        content=content,
        media_type=exporter.media_type,
        headers={
            # Both forms: `filename` for older clients, RFC 5987 `filename*` for non-ASCII names.
            "Content-Disposition": (
                f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
            ),
            "Content-Length": str(len(content)),
            # Exports are derived from a client-supplied body; caching them serves no purpose.
            "Cache-Control": "no-store",
            # Lets the browser read the header when the response is fetched cross-origin.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )
