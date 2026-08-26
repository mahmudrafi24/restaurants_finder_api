"""Application factory, middleware and exception handlers.

``uvicorn app.main:app`` locally; ``api/index.py`` re-exports the same object for Vercel.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.v1.router import router as v1_router
from app.config import VERSION, Settings, get_settings
from app.core.errors import AppError
from app.core.http import UpstreamClient
from app.core.logging import configure_logging
from app.services.geocoding import GeocodingService
from app.services.overpass import OverpassService
from app.services.places import PlaceSearchService
from app.services.suggest import SuggestionService

logger = logging.getLogger(__name__)

DESCRIPTION = """
Collects cafe, restaurant and fast-food data from OpenStreetMap and returns it as JSON or as a
downloadable XLSX, CSV or GeoJSON file.

**Typical flow**

1. `GET /api/v1/cities/search?q=hamb` — resolve what the user typed into a real place.
2. `POST /api/v1/places/search` — fetch every matching amenity within a radius.
3. `POST /api/v1/places/export` — post those rows back to download a file.

Data © OpenStreetMap contributors, available under the
[ODbL](https://www.openstreetmap.org/copyright).
"""


async def _keep_awake_task() -> None:
    """Pings the external Render URL to prevent the free tier from sleeping."""
    external_url = os.getenv("RENDER_EXTERNAL_URL")
    if not external_url:
        return
        
    health_url = f"{external_url.rstrip('/')}/api/v1/health"
    import httpx
    
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)  # 10 minutes
            try:
                await client.get(health_url, timeout=10.0)
                logger.debug("pinged self to stay awake", extra={"url": health_url})
            except Exception as exc:
                logger.warning("self-ping failed", exc_info=exc)



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the shared HTTP client and services once per process, and close them cleanly."""
    settings: Settings = app.state.settings

    upstream = UpstreamClient(settings)
    geocoding = GeocodingService(upstream, settings)
    overpass = OverpassService(upstream, settings)

    app.state.upstream_client = upstream
    app.state.geocoding_service = geocoding
    app.state.suggestion_service = SuggestionService(upstream, settings)
    app.state.place_search_service = PlaceSearchService(geocoding, overpass)

    if not settings.contact_email:
        # Nominatim's policy asks for a contact address and rejects placeholder ones outright.
        # The app URL alone is accepted, so this is a warning rather than a hard failure.
        logger.warning(
            "CONTACT_EMAIL is not set; requests identify this deployment by URL only. "
            "Set a real address to comply with the Nominatim usage policy."
        )

    logger.info(
        "application started",
        extra={"environment": settings.environment, "version": VERSION},
    )
    
    awake_task = asyncio.create_task(_keep_awake_task())
    try:
        yield
    finally:
        awake_task.cancel()
        await upstream.aclose()
        logger.info("application stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a configured application instance.

    Accepting settings as an argument keeps tests from having to mutate the environment.
    """
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_output=settings.is_production)

    app = FastAPI(
        title=settings.app_name,
        version=VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        # Errors use a custom envelope; suppress the stock 422 schema so the docs stay accurate.
        responses={422: {"description": "Validation error"}},
    )
    app.state.settings = settings

    _register_middleware(app, settings)
    _register_exception_handlers(app)

    app.include_router(v1_router, prefix=settings.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """Send browsers hitting the bare domain to the interactive docs."""
        return RedirectResponse(url="/docs")

    return app


def _register_middleware(app: FastAPI, settings: Settings) -> None:
    """Install CORS.

    Rate limiting would be added here. It is deliberately not enabled: Overpass applies a
    fair-use policy, so a public deployment without per-IP limits risks having its traffic
    throttled. Adding `slowapi` or an equivalent at this point requires no route changes.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=settings.cors_origin_regex,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        # The browser cannot read Content-Disposition on a cross-origin fetch unless it is
        # explicitly exposed — without this the download filename is lost.
        expose_headers=["Content-Disposition"],
        max_age=3600,
    )


def _register_exception_handlers(app: FastAPI) -> None:
    """Translate exceptions into the API's error envelope."""

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        logger.info(
            "request failed",
            extra={"error_code": exc.code, "status_code": exc.status_code},
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "The request payload is invalid.",
                    # jsonable_encoder-safe: pydantic errors can carry non-serialisable context.
                    "details": {"fields": _describe_validation_errors(exc)},
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred. Please try again.",
                }
            },
        )


def _describe_validation_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    """Reduce pydantic's error list to a JSON-safe field/message summary."""
    return [
        {
            "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
            "message": str(error.get("msg", "")),
        }
        for error in exc.errors()
    ]


app = create_app()
