"""City lookup / autocomplete."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BeforeValidator

from app.api.deps import GeocodingDep, SuggestionDep
from app.core.errors import UpstreamError
from app.schemas.city import CitySearchResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cities", tags=["cities"])


@router.get(
    "/search",
    response_model=CitySearchResponse,
    summary="Find places matching a partial name",
)
async def search_cities(
    suggestions: SuggestionDep,
    geocoding: GeocodingDep,
    q: Annotated[
        str,
        BeforeValidator(lambda value: value.strip() if isinstance(value, str) else value),
        Query(
            min_length=2,
            max_length=200,
            description="Partial place name. Clients should debounce input before calling.",
        ),
    ],
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> CitySearchResponse:
    """Return candidate places for ``q``, best match first.

    Served by Photon, which supports partial matching. If Photon is disabled or failing, this
    degrades to Nominatim rather than erroring: suggestions get worse for partial input, but the
    field still works for a fully typed city name.

    An unmatched query yields an empty ``results`` list rather than a 404 — an autocomplete
    request for a half-typed name is a normal state, not an error.
    """
    if suggestions.enabled:
        try:
            return CitySearchResponse(query=q, results=await suggestions.suggest(q, limit=limit))
        except UpstreamError as exc:
            logger.warning(
                "suggestion upstream failed; falling back to Nominatim",
                extra={"error_code": exc.code},
            )

    return CitySearchResponse(query=q, results=await geocoding.search(q, limit=limit))
