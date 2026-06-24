"""Tests for observer/session.py."""

import pytest
import websockets

from observer.session import SessionManager


class FakeWebSocket:
    """Minimal fake websocket for unit testing."""

    def __init__(self, open_state: bool = True):
        self.open = open_state
        self.sent: list[str | bytes] = []
        self.closed = False

    async def send(self, message: str | bytes) -> None:
        if not self.open:
            raise websockets.exceptions.ConnectionClosed(None, None)  # type: ignore[arg-type]
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True
        self.open = False

    def __aiter__(self):
        return iter([]).__aiter__()

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_add_and_count():
    sm = SessionManager()
    ws = FakeWebSocket()
    await sm.add("c1", ws)
    assert sm.count == 1
    assert "c1" in sm.connections


@pytest.mark.asyncio
async def test_send_unicast():
    sm = SessionManager()
    ws = FakeWebSocket()
    await sm.add("c1", ws)
    assert await sm.send("c1", "hello") is True
    assert ws.sent == ["hello"]


@pytest.mark.asyncio
async def test_send_to_missing_client():
    sm = SessionManager()
    assert await sm.send("missing", "hello") is False


@pytest.mark.asyncio
async def test_broadcast():
    sm = SessionManager()
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    await sm.add("c1", ws1)
    await sm.add("c2", ws2)
    results = await sm.broadcast("msg")
    assert results == {"c1": True, "c2": True}
    assert ws1.sent == ["msg"]
    assert ws2.sent == ["msg"]


@pytest.mark.asyncio
async def test_remove():
    sm = SessionManager()
    ws = FakeWebSocket()
    await sm.add("c1", ws)
    await sm.remove("c1")
    assert sm.count == 0
    assert ws.closed is True


@pytest.mark.asyncio
async def test_close_all():
    sm = SessionManager()
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    await sm.add("c1", ws1)
    await sm.add("c2", ws2)
    await sm.close_all()
    assert sm.count == 0
    assert ws1.closed and ws2.closed


@pytest.mark.asyncio
async def test_replace_existing_connection():
    sm = SessionManager()
    old = FakeWebSocket()
    new = FakeWebSocket()
    await sm.add("c1", old)
    await sm.add("c1", new)
    assert sm.connections["c1"] is new
    assert old.closed is True
