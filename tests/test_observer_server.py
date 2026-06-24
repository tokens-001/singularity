"""Observer WebSocket Server 测试。"""

import asyncio
import json
import pytest
import websockets
from singularity.observer.server import ObserverServer, ConnectionManager, DEFAULT_PORT


@pytest.fixture
async def server():
    """启动测试用的 WebSocket 服务。"""
    srv = ObserverServer(host="127.0.0.1", port=DEFAULT_PORT + 1)
    await srv.start()
    yield srv
    await srv.stop()


@pytest.mark.asyncio
async def test_connection_and_welcome(server):
    """测试客户端连接后收到欢迎消息。"""
    uri = f"ws://127.0.0.1:{DEFAULT_PORT + 1}"
    async with websockets.connect(uri) as ws:
        # 应该收到欢迎消息
        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
        data = json.loads(msg)
        assert data["event"] == "welcome"
        assert "client_id" in data["data"]
        assert data["data"]["server"] == "singularity-observer"


@pytest.mark.asyncio
async def test_ping_pong(server):
    """测试 ping-pong 心跳。"""
    uri = f"ws://127.0.0.1:{DEFAULT_PORT + 1}"
    async with websockets.connect(uri) as ws:
        # 跳过欢迎消息
        await ws.recv()
        
        # 发送 ping
        await ws.send(json.dumps({"action": "ping"}))
        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
        data = json.loads(msg)
        assert data["event"] == "pong"
        assert "ts" in data["data"]


@pytest.mark.asyncio
async def test_subscribe(server):
    """测试订阅频道。"""
    uri = f"ws://127.0.0.1:{DEFAULT_PORT + 1}"
    async with websockets.connect(uri) as ws:
        await ws.recv()  # 跳过欢迎消息
        
        # 订阅频道
        await ws.send(json.dumps({
            "action": "subscribe",
            "channels": ["tasks", "logs"]
        }))
        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
        data = json.loads(msg)
        assert data["event"] == "subscribed"
        assert set(data["data"]["channels"]) == {"tasks", "logs"}


@pytest.mark.asyncio
async def test_broadcast(server):
    """测试广播消息。"""
    uri = f"ws://127.0.0.1:{DEFAULT_PORT + 1}"
    async with websockets.connect(uri) as ws:
        await ws.recv()  # 跳过欢迎消息
        
        # 服务端广播
        sent = await server.broadcast("test_event", {"key": "value"})
        assert sent == 1
        
        # 客户端接收
        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
        data = json.loads(msg)
        assert data["event"] == "test_event"
        assert data["data"]["key"] == "value"


@pytest.mark.asyncio
async def test_client_count(server):
    """测试客户端计数。"""
    assert server.client_count == 0
    
    uri = f"ws://127.0.0.1:{DEFAULT_PORT + 1}"
    async with websockets.connect(uri) as ws1:
        await ws1.recv()
        assert server.client_count == 1
        
        async with websockets.connect(uri) as ws2:
            await ws2.recv()
            assert server.client_count == 2
        
        # ws2 断开后，计数应该减少（需要一点时间清理）
        await asyncio.sleep(0.1)
        assert server.client_count == 1
    
    await asyncio.sleep(0.1)
    assert server.client_count == 0


def test_connection_manager():
    """测试 ConnectionManager 基本功能。"""
    manager = ConnectionManager(max_clients=2)
    assert manager.count == 0
    
    # 模拟连接（需要 mock ServerConnection）
    # 这里只测试基本逻辑，实际连接测试在上面的集成测试中
