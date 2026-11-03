"""Shared async HTTP client with retry/backoff for the OSM upstreams.

A single :class:`httpx.AsyncClient` is created at application startup and reused for every
request, so connections are pooled rather than re-established per call. Retries cover the
transient failures Overpass is prone to under load (429, 502, 503, 504) and honour
``Retry-After`` when the server supplies it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import TYPE_CHECKING, Any, Final

import httpx

from app.core.errors import (
    UpstreamError,
    UpstreamRateLimitedError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

#: Status codes worth retrying — all transient, none caused by a malformed request.
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 502, 503, 504})

#: Upper bound on a server-supplied ``Retry-After``. Overpass occasionally asks for minutes,
#: which is longer than any serverless function may wait.
MAX_RETRY_AFTER_SECONDS: Final[float] = 5.0


class UpstreamClient:
    """Thin wrapper adding retry, error translation and a policy-compliant User-Agent."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._nominatim_lock = asyncio.Lock()
        self._next_geocode_at = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout_seconds, connect=10.0),
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        service: str,
        params: dict[str, Any] | None = None,
        content: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Perform a request, retrying transient failures.

        Args:
            service: Human-readable upstream name, used in error messages.

        Raises:
            UpstreamTimeoutError: The upstream did not respond in time.
            UpstreamRateLimitedError: The upstream rate limited us and retries were exhausted.
            UpstreamUnavailableError: The upstream is down or unreachable.
            UpstreamError: Any other non-success response.
        """
        attempts = self._settings.max_retries + 1
        last_error: UpstreamError | None = None

        for attempt in range(1, attempts + 1):
            try:
                async with asyncio.timeout(self._settings.upstream_timeout_seconds):
                    if service == "Nominatim":
                        async with self._nominatim_lock:
                            await asyncio.sleep(max(0.0, self._next_geocode_at - time.monotonic()))
                            self._next_geocode_at = time.monotonic() + 1.0
                    response = await self._client.request(
                        method, url, params=params, content=content, headers=headers
                    )
            except (httpx.TimeoutException, TimeoutError):
                # The CLI only caught HTTPError, so a socket timeout killed the whole run.
                last_error = UpstreamTimeoutError(service)
            except httpx.HTTPError as exc:
                logger.warning("%s transport error on attempt %d: %s", service, attempt, exc)
                last_error = UpstreamUnavailableError(service)
            else:
                if response.is_success:
                    return response

                status = response.status_code
                if status not in RETRYABLE_STATUS_CODES:
                    # Non-retryable: fail immediately with the upstream's own status.
                    raise self._translate(service, response)

                last_error = self._translate(service, response)
                if attempt < attempts:
                    await asyncio.sleep(self._delay_for(response, attempt))
                    continue

            if attempt < attempts:
                await asyncio.sleep(self._delay_for(None, attempt))

        raise last_error or UpstreamUnavailableError(service)

    def _delay_for(self, response: httpx.Response | None, attempt: int) -> float:
        """Backoff delay, preferring the server's ``Retry-After`` when present."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    value = float(retry_after)
                    if math.isfinite(value):
                        return max(0.0, min(value, MAX_RETRY_AFTER_SECONDS))
                except ValueError:
                    pass  # HTTP-date form; fall through to exponential backoff
        return self._settings.retry_backoff_seconds * (2.0 ** (attempt - 1))

    @staticmethod
    def _translate(service: str, response: httpx.Response) -> UpstreamError:
        """Map an unsuccessful response onto a domain error."""
        status = response.status_code
        if status == 429:
            retry_after: float | None = None
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    value = float(raw)
                    retry_after = max(0.0, value) if math.isfinite(value) else None
                except ValueError:
                    retry_after = None
            return UpstreamRateLimitedError(service, retry_after)
        if status in (504, 408):
            return UpstreamTimeoutError(service)
        if status in (502, 503):
            return UpstreamUnavailableError(service)
        logger.warning(
            "%s returned HTTP %d: %s", service, status, response.text[:200].replace("\n", " ")
        )
        return UpstreamError(service, f"{service} returned HTTP {status}.")
