"""Observer WebSocket Server — 实时事件推送与指令接收。

基于 websockets 库实现，与 Flask SSE 层并行运行。
支持：
  - 客户端连接/断开管理
  - 事件广播（供调度循环推送）
  - 消息路由（客户端 → 服务端指令处理）
  - 心跳保活
  - 优雅关闭
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

from . import config

logger = logging.getLogger("singularity.observer")

# ── 常量（集中由 observer/config.py 管理）──────────────────
DEFAULT_HOST = config.DEFAULT_HOST
DEFAULT_PORT = config.DEFAULT_PORT
HEARTBEAT_INTERVAL = config.HEARTBEAT_INTERVAL
HEARTBEAT_TIMEOUT = config.HEARTBEAT_TIMEOUT
MAX_MESSAGE_SIZE = config.MAX_MESSAGE_SIZE
MAX_CLIENTS = config.MAX_CLIENTS
ALLOWED_EVENT_CHANNELS = config.ALLOWED_EVENT_CHANNELS


# ── 客户端会话 ────────────────────────────────────────────
@dataclass
class ClientSession:
    """单个 WebSocket 客户端的连接元数据。"""

    ws: ServerConnection
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    client_id: str = ""
    subscriptions: set[str] = field(default_factory=set)  # 订阅的事件频道

    @property
    def remote(self) -> str:
        return self.ws.remote_address[0] if self.ws.remote_address else "?"


# ── 连接管理器 ────────────────────────────────────────────
class ConnectionManager:
    """管理所有活跃的 WebSocket 连接，提供广播与定向推送。"""

    def __init__(self, max_clients: int = MAX_CLIENTS) -> None:
        self._clients: dict[str, ClientSession] = {}
        self._max_clients = max_clients
        self._next_id = 0

    @property
    def count(self) -> int:
        return len(self._clients)

    def add(self, ws: ServerConnection) -> ClientSession:
        """注册新连接，返回会话对象。"""
        if len(self._clients) >= self._max_clients:
            raise ConnectionError(f"已达最大连接数 {self._max_clients}")
        self._next_id += 1
        cid = f"ws-{self._next_id:04d}"
        session = ClientSession(ws=ws, client_id=cid)
        self._clients[cid] = session
        logger.info("客户端连接: %s (总计 %d)", cid, len(self._clients))
        return session

    def remove(self, cid: str) -> None:
        """移除已断开的连接。"""
        session = self._clients.pop(cid, None)
        if session:
            logger.info("客户端断开: %s (总计 %d)", cid, len(self._clients))

    def get(self, cid: str) -> ClientSession | None:
        return self._clients.get(cid)

    async def broadcast(self, event: str, data: Any, channels: set[str] | None = None) -> int:
        """向所有（或指定频道的）客户端广播事件。返回成功推送数。"""
        payload = json.dumps({"event": event, "data": data, "ts": time.time()}, ensure_ascii=False)
        sent = 0
        dead: list[str] = []
        for cid, session in self._clients.items():
            # 频道过滤：如果指定了 channels，只推给订阅了对应频道的客户端
            if channels and not (channels & session.subscriptions):
                continue
            try:
                await session.ws.send(payload)
                session.last_seen = time.time()
                sent += 1
            except websockets.ConnectionClosed:
                dead.append(cid)
        # 清理已断开的连接
        for cid in dead:
            self.remove(cid)
        return sent

    async def send_to(self, cid: str, event: str, data: Any) -> bool:
        """向指定客户端推送消息。"""
        session = self._clients.get(cid)
        if not session:
            return False
        payload = json.dumps({"event": event, "data": data, "ts": time.time()}, ensure_ascii=False)
        try:
            await session.ws.send(payload)
            session.last_seen = time.time()
            return True
        except websockets.ConnectionClosed:
            self.remove(cid)
            return False


# ── 消息处理 ──────────────────────────────────────────────
async def _handle_message(
    session: ClientSession,
    raw: str,
    manager: ConnectionManager,
    loop: asyncio.AbstractEventLoop,
) -> dict | None:
    """解析并路由客户端发来的消息。返回响应 dict 或 None。"""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return {"event": "error", "data": {"message": "无效的 JSON"}}

    action = msg.get("action", "")

    if action == "ping":
        return {"event": "pong", "data": {"ts": time.time()}}

    if action == "subscribe":
        channels = msg.get("channels", [])
        if isinstance(channels, list):
            session.subscriptions.update(
                ch for ch in channels if ch in ALLOWED_EVENT_CHANNELS
            )
        return {"event": "subscribed", "data": {"channels": list(session.subscriptions)}}

    if action == "unsubscribe":
        channels = msg.get("channels", [])
        session.subscriptions -= set(channels)
        return {"event": "unsubscribed", "data": {"channels": list(session.subscriptions)}}

    if action == "whoami":
        return {"event": "identity", "data": {"client_id": session.client_id, "remote": session.remote}}

    if action == "config":
        return {"event": "config", "data": config.as_dict()}

    if action == "chat":
        question = (msg.get("question") or "").strip()
        if not question:
            return {"event": "error", "data": {"message": "问题不能为空"}}

        def _on_answer(payload: dict) -> None:
            text = (payload.get("params") or {}).get("text", "")
            asyncio.run_coroutine_threadsafe(
                manager.send_to(session.client_id, "chat_answer",
                                {"text": text, "ts": time.time()}),
                loop,
            )

        try:
            from singularity.scheduler.observer_agent import submit_question
            submit_question(session.client_id, question, _on_answer)
        except Exception as e:
            return {"event": "error", "data": {"message": f"Observer 提交失败: {e}"}}

        return {"event": "chat_received", "data": {"question": question, "ts": time.time()}}

    return {"event": "error", "data": {"message": f"未知 action: {action}"}}


# ── 单连接处理器 ──────────────────────────────────────────
async def _handler(ws: ServerConnection, manager: ConnectionManager) -> None:
    """单个 WebSocket 连接的生命周期处理。"""
    session = manager.add(ws)

    # 发送欢迎消息
    await manager.send_to(session.client_id, "welcome", {
        "client_id": session.client_id,
        "server": "singularity-observer",
        "protocol": 1,
    })

    loop = asyncio.get_running_loop()
    try:
        async for raw in ws:
            if not isinstance(raw, str):
                await manager.send_to(session.client_id, "error", {"message": "仅支持文本消息"})
                continue
            response = await _handle_message(session, raw, manager, loop)
            if response:
                await manager.send_to(session.client_id, response["event"], response["data"])
    except websockets.ConnectionClosed:
        pass
    finally:
        manager.remove(session.client_id)


# ── 服务端主体 ────────────────────────────────────────────
class ObserverServer:
    """WebSocket 服务端，可独立运行或嵌入调度循环。"""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        max_clients: int = MAX_CLIENTS,
    ) -> None:
        self.host = host
        self.port = port
        self.manager = ConnectionManager(max_clients=max_clients)
        self._server = None
        self._heartbeat_task: asyncio.Task | None = None

    async def _heartbeat_loop(self) -> None:
        """定期心跳，清理死连接。"""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await self.manager.broadcast("heartbeat", {"ts": time.time()})

    async def start(self) -> None:
        """启动 WebSocket 服务。"""
        self._server = await serve(
            lambda ws: _handler(ws, self.manager),
            self.host,
            self.port,
            max_size=MAX_MESSAGE_SIZE,
            ping_interval=HEARTBEAT_INTERVAL,
            ping_timeout=HEARTBEAT_TIMEOUT,
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("Observer WebSocket 服务已启动: ws://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        """优雅关闭：停止接收新连接，等待现有连接结束。"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Observer WebSocket 服务已关闭")

    async def broadcast(self, event: str, data: Any, channels: set[str] | None = None) -> int:
        """外部调用接口：向所有客户端广播事件。"""
        return await self.manager.broadcast(event, data, channels)

    @property
    def client_count(self) -> int:
        return self.manager.count


# ── 全局单例（供调度循环直接调用）─────────────────────────
_server: ObserverServer | None = None


def get_server() -> ObserverServer | None:
    """获取全局 ObserverServer 实例。"""
    return _server


def init_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ObserverServer:
    """初始化全局 ObserverServer 单例。"""
    global _server
    _server = ObserverServer(host=host, port=port)
    return _server


# ── 独立运行入口 ──────────────────────────────────────────
async def _standalone() -> None:
    """独立模式：直接运行 WebSocket 服务。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    server = ObserverServer()
    await server.start()
    try:
        await asyncio.Future()  # 永久运行
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


def main() -> None:
    """CLI 入口。"""
    asyncio.run(_standalone())


if __name__ == "__main__":
    main()
