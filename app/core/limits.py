"""Request admission, byte limits and total deadlines for expensive endpoints.

Rate limits are per process. Multiple workers need a shared ingress limit and trusted proxy
configuration; arbitrary forwarded-IP headers are deliberately ignored here.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from app.core.errors import UpstreamTimeoutError
from app.schemas.place import MAX_REQUEST_BYTES

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from app.config import Settings

_RATE_WINDOW_SECONDS = 60
_MAX_TRACKED_CLIENTS = 4096


class _ClientRateLimiter:
    """Track a bounded sliding window of request timestamps for each client."""

    def __init__(self, requests_per_minute: int) -> None:
        self.limit = requests_per_minute
        self.clients: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        requests = self.clients.setdefault(client, deque())
        self.clients.move_to_end(client)
        while requests and requests[0] <= now - _RATE_WINDOW_SECONDS:
            requests.popleft()
        if len(requests) >= self.limit:
            return False
        requests.append(now)
        if len(self.clients) > _MAX_TRACKED_CLIENTS:
            self.clients.popitem(last=False)
        return True


class _BodyTooLargeError(Exception):
    """Stop reading as soon as the actual received bytes exceed the budget."""


async def _read_body(receive: Receive) -> bytes | None:
    """Return a bounded body, or None when the client disconnects before it is complete."""
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body", b"")
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise _BodyTooLargeError
        body.extend(chunk)
        if not message.get("more_body", False):
            return bytes(body)


def _replay_body(body: bytes, receive: Receive) -> Receive:
    """Deliver the buffered body once, then pass through subsequent receive events."""
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if delivered:
            return await receive()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status,
        headers={"Retry-After": str(_RATE_WINDOW_SECONDS)} if status == 429 else None,
    )


class RequestLimitsMiddleware:
    """Reject overload before JSON parsing; one deadline covers all provider retries."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.slots = asyncio.Semaphore(settings.max_concurrent_requests)
        self.rate_limiter = _ClientRateLimiter(settings.requests_per_minute)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].endswith(("/search", "/export")):
            await self.app(scope, receive, send)
            return

        client = str((scope.get("client") or ("unknown",))[0])
        if not self.rate_limiter.allow(client):
            response = _error_response(
                429, "rate_limited", "Too many requests. Wait a minute and try again."
            )
            await response(scope, receive, send)
            return
        if self.slots.locked():
            response = _error_response(
                503, "server_busy", "The server is busy. Please try again shortly."
            )
            await response(scope, receive, send)
            return
        async with self.slots:
            await self._dispatch(scope, receive, send)

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        response_started = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                body = await _read_body(receive)
                if body is not None:
                    await self.app(scope, _replay_body(body, receive), tracked_send)
        except _BodyTooLargeError:
            response = _error_response(413, "request_too_large", "Request exceeds the 4 MB limit.")
            await response(scope, receive, send)
        except TimeoutError:
            # Once headers are sent, a second response would violate the ASGI protocol.
            if response_started:
                raise
            error = UpstreamTimeoutError("The search service")
            await JSONResponse(error.to_payload(), status_code=504)(scope, receive, send)
