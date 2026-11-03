"""Per-process admission control, bounded request bodies and a total request deadline.

Limits are per worker. Multi-worker deployments must also enforce a shared rate limit at
an ingress proxy. No forwarded-IP header is trusted without configuring that proxy first.
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


class RequestLimitsMiddleware:
    """Reject overload before parsing JSON; the deadline covers all upstream retries."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.slots = asyncio.Semaphore(settings.max_concurrent_requests)
        self.clients: OrderedDict[str, deque[float]] = OrderedDict()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].endswith(("/search", "/export")):
            await self.app(scope, receive, send)
            return

        async def reject(status: int, code: str, message: str) -> None:
            await JSONResponse(
                {"error": {"code": code, "message": message}},
                status_code=status,
                headers={"Retry-After": "60"} if status == 429 else None,
            )(scope, receive, send)

        now = time.monotonic()
        client = str((scope.get("client") or ("unknown",))[0])
        requests = self.clients.setdefault(client, deque())
        self.clients.move_to_end(client)
        while requests and requests[0] <= now - 60:
            requests.popleft()
        if len(requests) >= self.settings.requests_per_minute:
            await reject(429, "rate_limited", "Too many requests. Wait a minute and try again.")
            return
        requests.append(now)
        if len(self.clients) > 4096:
            self.clients.popitem(last=False)
        if self.slots.locked():
            await reject(503, "server_busy", "The server is busy. Please try again shortly.")
            return

        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async with self.slots:
            try:
                async with asyncio.timeout(self.settings.request_timeout_seconds):
                    # Buffer only a bounded body. Counting chunks also covers requests with a
                    # missing, invalid or dishonest Content-Length header.
                    body = bytearray()
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                            await reject(
                                413, "request_too_large", "Request exceeds the 4 MB limit."
                            )
                            return
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
                    delivered = False

                    async def replay() -> Message:
                        nonlocal delivered
                        if not delivered:
                            delivered = True
                            return {"type": "http.request", "body": bytes(body), "more_body": False}
                        return await receive()

                    await self.app(scope, replay, tracked_send)
            except TimeoutError:
                if started:
                    raise
                error = UpstreamTimeoutError("The search service")
                await JSONResponse(error.to_payload(), status_code=504)(scope, receive, send)
