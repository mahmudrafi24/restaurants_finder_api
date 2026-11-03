"""Bounded place results and per-key coordination for concurrent searches."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from app.core.cache import TTLCache
from app.schemas.place import Place

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_PLACES = TypeAdapter(list[Place])


@dataclass
class _Pending:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class PlaceResultCache:
    """Cache only complete successes, with a serialized byte budget for each entry.

    Store bytes rather than mutable model graphs, bounding retained payload memory and
    returning independent objects to callers. Unrelated searches never share a lock.
    """

    def __init__(self, *, ttl_seconds: int, max_entries: int, max_entry_bytes: int) -> None:
        self._cache = TTLCache[bytes](ttl_seconds=ttl_seconds, max_entries=max_entries)
        self._max_entry_bytes = max_entry_bytes
        self._pending: dict[str, _Pending] = {}

    async def get_or_load(
        self, key: str, loader: Callable[[], Awaitable[list[Place]]]
    ) -> list[Place]:
        cached = await self._cache.get(key)
        if cached is not None:
            return _PLACES.validate_json(cached)

        pending = self._pending.setdefault(key, _Pending())
        pending.users += 1
        try:
            async with pending.lock:
                cached = await self._cache.get(key)
                if cached is not None:
                    return _PLACES.validate_json(cached)
                places = await loader()
                encoded = _PLACES.dump_json(places)
                if len(encoded) <= self._max_entry_bytes:
                    await self._cache.set(key, encoded)
                return places
        finally:
            # A cancelled leader releases the lock so a waiter can take over. Failed
            # requests aren't cached; discard idle locks to avoid unbounded key growth.
            pending.users -= 1
            if pending.users == 0:
                del self._pending[key]
