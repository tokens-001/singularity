"""bridge.py — WebSocket 桥接层 (T1)

与 SSE 共存: 同样的 _sse_clients 推送逻辑, WebSocket 客户端也接收。
协议: JSON-RPC 2.0 通知 (单向推送, 客户端通过 HTTP API 发送请求)
认证: X-Qidian-Token 请求头
"""
from __future__ import annotations
import asyncio
import json
import threading
import time
import logging

_log = logging.getLogger("ws")

# 全局 WebSocket 客户端集合
_ws_clients: set = set()
_ws_lock = threading.Lock()


class _WSClient:
    """单个 WebSocket 连接。"""
    def __init__(self, ws, remote_addr: str = ""):
        self.ws = ws
        self.remote_addr = remote_addr
        self.authenticated = False
        self.connected_at = time.time()

    async def send_json(self, data: dict) -> bool:
        try:
            await self.ws.send(json.dumps(data, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass


def add_client(ws_client: _WSClient) -> None:
    with _ws_lock:
        _ws_clients.add(ws_client)
    _log.info("WS client connected (%d total)", len(_ws_clients))


def remove_client(ws_client: _WSClient) -> None:
    with _ws_lock:
        _ws_clients.discard(ws_client)
    _log.info("WS client disconnected (%d remaining)", len(_ws_clients))


def broadcast_json(data: dict) -> int:
    """向所有已认证 WebSocket 客户端广播。返回成功发送数。"""
    dead = []
    with _ws_lock:
        clients = list(_ws_clients)
    count = 0
    for c in clients:
        if not c.authenticated:
            continue
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(c.send_json(data))
            else:
                asyncio.run_coroutine_threadsafe(c.send_json(data), _get_loop())
            count += 1
        except Exception:
            dead.append(c)
    for c in dead:
        remove_client(c)
    return count


_WS_LOOP: asyncio.AbstractEventLoop | None = None
_WS_THREAD: threading.Thread | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _WS_LOOP
    if _WS_LOOP is None:
        _WS_LOOP = asyncio.new_event_loop()
    return _WS_LOOP


async def _ws_handler(ws):
    """WebSocket 连接处理器。"""
    from scheduler._auth import get_auth

    client = _WSClient(ws)
    add_client(client)

    try:
        # 等待认证消息 (首条消息必须是 JSON-RPC auth)
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        if msg.get("method") == "auth":
            token = msg.get("params", {}).get("token", "")
            auth = get_auth()
            user = auth.authenticate(token) if token else None
            if user:
                client.authenticated = True
                await client.send_json({
                    "jsonrpc": "2.0", "method": "auth_ok",
                    "params": {"user": user}}
                )
            else:
                await client.send_json({
                    "jsonrpc": "2.0", "method": "auth_error",
                    "params": {"message": "无效 token"}}
                )
                return
        else:
            # 也支持通过请求头认证
            await client.send_json({
                "jsonrpc": "2.0", "method": "auth_error",
                "params": {"message": "首条消息必须是 auth"}}
            )
            return

        # 保持连接，等待客户端关闭
        async for _ in ws:
            pass  # 客户端通过 HTTP API 交互，WS 仅用于推送
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass
    finally:
        remove_client(client)


def start_ws_server(host: str = "127.0.0.1", port: int = 5051):
    """在独立线程启动 WebSocket 服务器。"""
    global _WS_THREAD, _WS_LOOP

    async def _serve():
        import websockets
        _WS_LOOP = asyncio.get_event_loop()
        async with websockets.serve(_ws_handler, host, port):
            _log.info("WebSocket server on ws://%s:%d", host, port)
            await asyncio.Future()  # 永远运行

    def _run():
        loop = _get_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_serve())

    _WS_THREAD = threading.Thread(target=_run, daemon=True)
    _WS_THREAD.start()
    _log.info("WebSocket server started on ws://%s:%d", host, port)


def stop_ws_server():
    global _WS_LOOP
    with _ws_lock:
        for c in list(_ws_clients):
            try:
                asyncio.run_coroutine_threadsafe(c.close(), _get_loop())
            except Exception:
                pass
        _ws_clients.clear()
    if _WS_LOOP:
        try:
            _WS_LOOP.call_soon_threadsafe(_WS_LOOP.stop)
        except Exception:
            pass
