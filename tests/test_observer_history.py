"""Tests for observer.history."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from observer.history import (
    HistoryCache,
    HistoryEntry,
    SessionHistory,
    get_history_cache,
    reset_history_cache,
)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    reset_history_cache()
    yield
    reset_history_cache()


@pytest.fixture
def cache(tmp_path: Path) -> HistoryCache:
    return HistoryCache(max_len=5, persistence_path=tmp_path / "history.json")


@pytest.mark.asyncio
async def test_session_history_ring_buffer() -> None:
    hist = SessionHistory("s1", max_len=3)
    for i in range(5):
        await hist.append(
            HistoryEntry(ts=float(i), direction="out", message={"idx": i})
        )

    recent = await hist.recent()
    assert len(recent) == 3
    assert [e.message["idx"] for e in recent] == [2, 3, 4]


@pytest.mark.asyncio
async def test_recent_filters(cache: HistoryCache) -> None:
    await cache.record_inbound("s1", {"event": "ping"})
    await cache.record_outbound("s1", {"event": "status"})
    await cache.record_outbound("s1", {"event": "ping"})

    all_entries = await cache.replay("s1")
    assert len(all_entries) == 3

    out_only = await (await cache.get("s1")).recent(direction="out")
    assert len(out_only) == 2

    pings = await (await cache.get("s1")).recent(event_type="ping")
    assert len(pings) == 2


@pytest.mark.asyncio
async def test_replay_unknown_session(cache: HistoryCache) -> None:
    assert await cache.replay("missing") == []


@pytest.mark.asyncio
async def test_persistence(cache: HistoryCache) -> None:
    await cache.record_outbound("s1", {"event": "hello"})
    await cache.record_inbound("s1", {"event": "reply"})
    await cache.save()

    fresh = HistoryCache(max_len=5, persistence_path=cache._persistence_path)
    restored = await fresh.load()
    assert restored == 1

    replay = await fresh.replay("s1")
    assert len(replay) == 2
    assert replay[0]["direction"] == "out"
    assert replay[1]["direction"] == "in"


@pytest.mark.asyncio
async def test_persistence_corrupt_file(cache: HistoryCache) -> None:
    cache._persistence_path.parent.mkdir(parents=True, exist_ok=True)
    cache._persistence_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        await cache.load()


@pytest.mark.asyncio
async def test_clear_all(cache: HistoryCache) -> None:
    await cache.record_outbound("s1", {"event": "x"})
    await cache.save()
    await cache.clear_all()
    assert await cache.count == 0
    assert not cache._persistence_path.exists()


@pytest.mark.asyncio
async def test_concurrent_appends() -> None:
    hist = SessionHistory("s1", max_len=100)

    async def worker(start: int) -> None:
        for i in range(50):
            await hist.append(
                HistoryEntry(ts=float(start + i), direction="out", message={"idx": start + i})
            )

    await asyncio.gather(worker(0), worker(50))
    assert await hist.count == 100


@pytest.mark.asyncio
async def test_global_singleton() -> None:
    a = get_history_cache()
    b = get_history_cache()
    assert a is b
