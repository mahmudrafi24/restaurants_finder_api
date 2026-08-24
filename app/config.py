"""Application settings, loaded from the environment with validation.

Import :func:`get_settings` rather than instantiating ``Settings`` directly — it is cached so
the environment is parsed once per process.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Final, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Self

Environment = Literal["development", "staging", "production"]

VERSION = "0.1.0"

#: Product token in the outgoing User-Agent.
#:
#: It must not contain "scraper". The Overpass API's request filter rejects any User-Agent
#: carrying that word with HTTP 406 before the query is ever parsed — verified live:
#: ``TestScraper/1.0`` is refused where ``TestFinder/1.0`` succeeds against an identical request.
#: The project name is unaffected; this is only the string sent upstream.
USER_AGENT_PRODUCT: Final = "RestaurantFinderAPI"

#: Substrings that make upstreams refuse traffic outright, guarded by a test so a future rename
#: cannot silently take Overpass offline.
BLOCKED_USER_AGENT_TOKENS: Final[frozenset[str]] = frozenset({"scraper", "crawler", "spider"})

#: Domains reserved for documentation (RFC 2606) plus common stand-ins. An address at any of
#: these in the User-Agent makes Nominatim reject every request with HTTP 403 — verified live.
PLACEHOLDER_EMAIL_DOMAINS: Final[frozenset[str]] = frozenset(
    {"example.com", "example.org", "example.net", "example.edu", "test", "localhost"}
)


class Settings(BaseSettings):
    """Runtime configuration. Every value can be overridden by an environment variable."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Application ──────────────────────────────────────────────────────────
    app_name: str = "Restaurant Scraper API"
    environment: Environment = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # ── CORS ─────────────────────────────────────────────────────────────────
    # `NoDecode` suppresses pydantic-settings' automatic JSON decoding of complex types, which
    # otherwise raises before the validator below can accept a comma-separated value:
    #   CORS_ORIGINS=http://localhost:3000,https://my-app.vercel.app
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    # Allows Vercel preview deployments (https://<project>-<hash>-<org>.vercel.app) without
    # having to enumerate every generated URL.
    cors_origin_regex: str | None = r"https://.*\.vercel\.app"

    # ── Upstream: contact identity ───────────────────────────────────────────
    # Nominatim's usage policy requires a User-Agent that identifies the application and offers
    # a way to make contact. It enforces this: a UA carrying a placeholder address (anything at
    # a reserved domain such as example.com) is answered with a blanket HTTP 403, so the default
    # here is "unset" rather than a fake address, and `_discard_placeholder_email` drops obvious
    # stand-ins instead of passing them upstream. Set CONTACT_EMAIL before deploying publicly.
    contact_email: str | None = None
    app_url: str = "https://github.com/mahmudrafi24/cafe_scraping"

    # ── Upstream: endpoints ──────────────────────────────────────────────────
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    overpass_base_url: str = "https://overpass-api.de/api/interpreter"
    # Comma-separated fallback Overpass mirrors. Tried in order when the primary URL fails.
    # The main overpass-api.de blocks many cloud providers; these mirrors generally don't.
    overpass_fallback_urls: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "https://overpass.private.coffee/api/interpreter",
            "https://overpass.openstreetmap.ru/api/interpreter",
        ]
    )
    # Photon is an OSM-based geocoder built for type-ahead search, used for city suggestions.
    # Nominatim matches whole names only — "hambu" finds a village in Papua New Guinea, never
    # Hamburg — so it cannot drive an autocomplete. Nominatim remains the authority for
    # resolving a submitted city name. Setting this empty disables suggestions, which then fall
    # back to Nominatim. Komoot's public instance is free; self-host it for heavy use.
    photon_base_url: str = "https://photon.komoot.io"
    # Language for place labels, sent to both geocoders. They otherwise return local-language
    # names ("ঢাকা" rather than "Dhaka"), which makes a mixed-locale result list hard to scan.
    preferred_language: str = "en"

    # ── Upstream: timeouts and retries ───────────────────────────────────────
    # Must stay below the platform's function timeout (Vercel: `maxDuration`, 60s here).
    upstream_timeout_seconds: float = 55.0
    # Value passed to Overpass' own `[timeout:N]` setting, in seconds. Kept under
    # `upstream_timeout_seconds` so Overpass reports a clean error before our socket gives up.
    overpass_query_timeout_seconds: int = 50
    max_retries: int = 2
    retry_backoff_seconds: float = 1.5

    # ── Search defaults ──────────────────────────────────────────────────────
    # Hard limits (radius ceiling, export row cap) live in app.schemas.place as part of the
    # public API contract, so pydantic enforces them and OpenAPI documents them.
    default_radius_km: Annotated[int, Field(ge=1, le=25)] = 10
    city_suggestion_limit: Annotated[int, Field(ge=1, le=20)] = 8

    # ── Geocoding cache (Nominatim policy compliance) ────────────────────────
    city_cache_ttl_seconds: int = 86_400  # city coordinates are effectively static
    city_cache_max_entries: int = 512

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: object) -> object:
        """Accept ``CORS_ORIGINS`` as a comma-separated string or as a JSON array.

        `NoDecode` (above) means nothing decodes JSON on our behalf, so both forms are handled
        here. Deployment platforms differ in which one is natural to enter, and getting this
        wrong takes the whole app down at startup rather than failing a single request.
        """
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass  # not valid JSON after all; fall through to comma-splitting
        return [origin.strip() for origin in stripped.split(",") if origin.strip()]

    @field_validator("overpass_fallback_urls", mode="before")
    @classmethod
    def _parse_fallback_urls(cls, value: object) -> object:
        """Accept ``OVERPASS_FALLBACK_URLS`` as comma-separated or JSON array."""
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
        return [url.strip() for url in stripped.split(",") if url.strip()]

    @property
    def overpass_urls(self) -> list[str]:
        """Primary Overpass URL followed by any fallbacks, deduplicated."""
        seen: set[str] = set()
        urls: list[str] = []
        for url in [self.overpass_base_url, *self.overpass_fallback_urls]:
            if url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    @field_validator("cors_origin_regex", mode="before")
    @classmethod
    def _empty_regex_is_none(cls, value: object) -> object:
        """Treat ``CORS_ORIGIN_REGEX=""`` as "disabled" rather than "match everything"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("contact_email", mode="before")
    @classmethod
    def _discard_placeholder_email(cls, value: object) -> object:
        """Drop placeholder contact addresses rather than sending them to Nominatim.

        Nominatim answers *every* request whose User-Agent contains an address at a reserved
        domain with HTTP 403. Silently omitting the address keeps the service working (the app
        URL still identifies us); :func:`app.main.create_app` warns when it is missing.
        """
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return None
        _, _, domain = stripped.rpartition("@")
        if domain.lower() in PLACEHOLDER_EMAIL_DOMAINS:
            return None
        return stripped

    @property
    def user_agent(self) -> str:
        """User-Agent sent to the upstreams, as their usage policies require.

        See :data:`USER_AGENT_PRODUCT` for the constraint on what may appear here.
        """
        identity = f"{USER_AGENT_PRODUCT}/{VERSION} (+{self.app_url}"
        if self.contact_email:
            identity += f"; {self.contact_email}"
        return f"{identity})"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
