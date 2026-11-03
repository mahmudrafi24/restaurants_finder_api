"""Bound workbook rendering independently of request cancellation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.schemas.place import Place
    from app.services.exporters.base import Exporter


async def render_export(
    exporter: Exporter,
    places: Sequence[Place],
    *,
    city_name: str,
    slots: asyncio.Semaphore,
) -> bytes:
    """Render in a thread, holding its slot until work finishes even if the caller leaves."""
    await slots.acquire()
    task = asyncio.create_task(asyncio.to_thread(exporter.render, places, city_name=city_name))

    def completed(rendered: asyncio.Task[bytes]) -> None:
        slots.release()
        if not rendered.cancelled():
            rendered.exception()  # retrieve failures even if the requesting task was cancelled

    task.add_done_callback(completed)
    return await asyncio.shield(task)
