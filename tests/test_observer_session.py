"""Tests for observer.session."""

from __future__ import annotations

import asyncio

import pytest

from observer.session import SessionContext, SessionStore, get_store, reset_store


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    reset_store()
    yield
    reset_store()


@pytest.fixture
def store() -> SessionStore:
    return get_store()


@pytest.mark.asyncio
async def test_create_and_get(store: SessionStore) -> None:
    ctx = await store.create("c1", {"ip": "127.0.0.1"})
    assert ctx.client_id == "c1"
    assert ctx.metadata["ip"] == "127.0.0.1"

    got = await store.get("c1")
    assert got is ctx


@pytest.mark.asyncio
async def test_remove(store: SessionStore) -> None:
    await store.create("c1")
    removed = await store.remove("c1")
    assert removed is not None
    assert removed.client_id == "c1"
    assert await store.get("c1") is None


@pytest.mark.asyncio
async def test_message_history(store: SessionStore) -> None:
    await store.create("c1")
    assert await store.record_message("c1", {"event": "ping"}) is True
    assert await store.record_message("c2", {"event": "ping"}) is False

    history = await store.get_history("c1")
    assert len(history) == 1
    assert history[0]["message"]["event"] == "ping"

    filtered = await store.get_history("c1", event_type="pong")
    assert len(filtered) == 0


@pytest.mark.asyncio
async def test_pending_action_lifecycle(store: SessionStore) -> None:
    await store.create("c1")
    payload = await store.add_pending_action("c1", "a1", {"type": "ask"})
    assert payload is not None
    assert payload["action_id"] == "a1"

    pending = await store.get_pending_actions("c1")
    assert len(pending) == 1

    confirmed = await store.confirm_action("c1", "a1", {"answer": "yes"})
    assert confirmed is not None
    assert confirmed["result"]["answer"] == "yes"
    assert await store.confirm_action("c1", "a1") is None


@pytest.mark.asyncio
async def test_pending_action_expiration(store: SessionStore) -> None:
    await store.create("c1")
    await store.add_pending_action("c1", "a1", {"type": "ask"}, timeout=0.01)
    await asyncio.sleep(0.02)
    expired = await store.expire_pending_actions("c1")
    assert len(expired) == 1
    assert expired[0]["action_id"] == "a1"
    assert await store.get_pending_actions("c1") == []


@pytest.mark.asyncio
async def test_subscriptions(store: SessionStore) -> None:
    await store.create("c1")
    subs = await store.subscribe("c1", ["metrics", "alerts"])
    assert subs == {"metrics", "alerts"}

    subs = await store.subscribe("c1", ["logs"])
    assert subs == {"metrics", "alerts", "logs"}

    subs = await store.unsubscribe("c1", ["alerts"])
    assert subs == {"metrics", "logs"}


@pytest.mark.asyncio
async def test_concurrent_access(store: SessionStore) -> None:
    await store.create("c1")

    async def worker(n: int) -> None:
        for i in range(n):
            await store.record_message("c1", {"idx": i})
            await store.add_pending_action("c1", f"a{i}", {"idx": i})
            await store.confirm_action("c1", f"a{i}")

    await asyncio.gather(worker(50), worker(50))
    history = await store.get_history("c1")
    assert len(history) == 100
    assert await store.get_pending_actions("c1") == []


@pytest.mark.asyncio
async def test_stale_sessions(store: SessionStore) -> None:
    await store.create("c1")
    await asyncio.sleep(0.02)
    stale = await store.stale_sessions(0.01)
    assert stale == ["c1"]
