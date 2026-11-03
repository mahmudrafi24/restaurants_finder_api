"""Shared search caching, isolation, expiry, and cancellation behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.errors import UpstreamError
from app.schemas.place import AmenityType, Place
from app.services.place_cache import PlaceResultCache
from app.services.places import PlaceSearchService


def cache(**overrides: int) -> PlaceResultCache:
    return PlaceResultCache(
        **{
            "ttl_seconds": 600,
            "max_entries": 2,
            "max_entry_bytes": 2_000_000,
            **overrides,
        }
    )


def venue() -> Place:
    return Place(osm_id="node/1", name="Cafe", amenity="cafe", latitude=0, longitude=0)


async def test_concurrent_successes_share_work_and_cached_models_are_independent() -> None:
    store = cache()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def load() -> list[Place]:
        entered.set()
        await finish.wait()
        return [venue()]

    loader = AsyncMock(side_effect=load)
    first = asyncio.create_task(store.get_or_load("same", loader))
    await entered.wait()
    second = asyncio.create_task(store.get_or_load("same", loader))
    await asyncio.sleep(0)
    finish.set()
    a, b = await asyncio.gather(first, second)
    a[0].name = "changed"
    assert b[0].name == "Cafe"
    assert loader.await_count == 1
    assert store._pending == {}


async def test_different_keys_do_not_wait_for_one_another() -> None:
    store = cache()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def slow() -> list[Place]:
        entered.set()
        await finish.wait()
        return [venue()]

    pending = asyncio.create_task(store.get_or_load("slow", slow))
    await entered.wait()
    try:
        assert (
            await asyncio.wait_for(store.get_or_load("fast", AsyncMock(return_value=[])), 1) == []
        )
    finally:
        finish.set()
        await pending


async def test_cancelled_leader_releases_waiter_and_cleans_up() -> None:
    store = cache()
    entered = asyncio.Event()

    async def wait_forever() -> list[Place]:
        entered.set()
        await asyncio.Event().wait()
        return []

    leader = asyncio.create_task(store.get_or_load("same", wait_forever))
    await entered.wait()
    waiter = asyncio.create_task(store.get_or_load("same", AsyncMock(return_value=[venue()])))
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert len(await asyncio.wait_for(waiter, 1)) == 1
    assert store._pending == {}


async def test_errors_are_not_cached_and_locks_are_released() -> None:
    store = cache()
    loader = AsyncMock(side_effect=[UpstreamError("test", "failed"), [venue()]])
    with pytest.raises(UpstreamError):
        await store.get_or_load("same", loader)
    assert store._pending == {}
    assert len(await store.get_or_load("same", loader)) == 1
    assert loader.await_count == 2


async def test_expiry_reloads_and_empty_success_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    store = cache()
    now = 1000.0
    monkeypatch.setattr("app.core.cache.time.monotonic", lambda: now)
    loader = AsyncMock(return_value=[])
    assert await store.get_or_load("empty", loader) == []
    assert await store.get_or_load("empty", loader) == []
    assert loader.await_count == 1
    now += 601
    assert await store.get_or_load("empty", loader) == []
    assert loader.await_count == 2


@pytest.mark.parametrize("options", [{"max_entry_bytes": 1}, {"ttl_seconds": 0}])
async def test_oversized_or_disabled_cache_does_not_retain_results(options: dict[str, int]) -> None:
    store = cache(**options)
    loader = AsyncMock(return_value=[venue()])
    for _ in range(2):
        assert len(await store.get_or_load("same", loader)) == 1
    assert loader.await_count == 2
    assert len(store._cache) == 0


async def test_entry_limit_evicts_old_results() -> None:
    store = cache(max_entries=1)
    loader = AsyncMock(return_value=[])
    for key in ("a", "b", "a"):
        await store.get_or_load(key, loader)
    assert loader.await_count == 3
    assert len(store._cache) == 1


async def test_search_cache_keys_preserve_geography_and_share_amenity_order() -> None:
    geocoding = AsyncMock()
    overpass = AsyncMock()
    overpass.fetch_amenities.return_value = []
    service = PlaceSearchService(geocoding, overpass)
    base = {
        "city": "Leipzig",
        "radius_km": 5,
        "coordinates": (51.34, 12.37),
        "amenities": [AmenityType.CAFE, AmenityType.RESTAURANT],
    }
    first = await service.search(**base)
    second = await service.search(
        **{**base, "city": "Leipzig alias", "amenities": list(reversed(base["amenities"]))}
    )
    assert first.city.name == "Leipzig"
    assert second.city.name == "Leipzig alias"
    assert overpass.fetch_amenities.await_count == 1
    for changed in (
        {"radius_km": 6},
        {"coordinates": (51.35, 12.37)},
        {"amenities": [AmenityType.CAFE]},
    ):
        await service.search(**{**base, **changed})
    assert overpass.fetch_amenities.await_count == 4
    geocoding.resolve.assert_not_called()
