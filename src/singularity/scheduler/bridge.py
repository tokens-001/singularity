"""bridge.py — WebSocket 桥接层 (T1)

与 SSE 共存: 同样的 _sse_clients 推送逻辑, WebSocket 客户端也接收。
协议: JSON-RPC 2.0 通知 (单向推送, 客户端通过 HTTP API 发送请求)
认证: X-Qidian-Token 请求头

T11: 集成 Observer Server，提供实时事件推送与指令接收能力。
"""
from __future__ import annotations
import asyncio
import json
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor

_log = logging.getLogger("ws")

# 全局 WebSocket 客户端集合
_ws_clients: set = set()
_ws_lock = threading.Lock()

# Observer Server 实例
_observer_server = None
_observer_thread: threading.Thread | None = None
_observer_loop: asyncio.AbstractEventLoop | None = None

# 共享线程池（供 bridge 与 observer 复用）
_executor: ThreadPoolExecutor | None = None


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


def get_executor() -> ThreadPoolExecutor:
    """获取或创建共享线程池。"""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ws-exec")
    return _executor


async def _ws_handler(ws):
    """WebSocket 连接处理器。"""
    from singularity.scheduler._auth import get_auth

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


# ── Observer Server 集成 (T11) ─────────────────────────────────────────────

def start_observer_server(
    host: str = "0.0.0.0",
    port: int = 8765,
    use_thread: bool = True,
) -> None:
    """启动 Observer Server（非阻塞）。

    Args:
        host: 监听地址
        port: 监听端口
        use_thread: True 使用独立线程，False 使用 asyncio.create_task（需已有事件循环）
    """
    global _observer_server, _observer_thread, _observer_loop

    from singularity.observer.server import ObserverServer

    if _observer_server is not None:
        _log.warning("Observer Server 已在运行")
        return

    _observer_server = ObserverServer(host=host, port=port)

    if use_thread:
        # 独立线程模式（推荐，与现有 bridge 一致）
        async def _run_observer():
            _observer_loop = asyncio.get_event_loop()
            await _observer_server.start()
            try:
                await asyncio.Future()  # 永久运行
            except asyncio.CancelledError:
                pass
            finally:
                await _observer_server.stop()

        def _thread_entry():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_run_observer())

        _observer_thread = threading.Thread(
            target=_thread_entry,
            daemon=True,
            name="observer-server"
        )
        _observer_thread.start()
        _log.info("Observer Server 已启动（线程模式）: ws://%s:%d", host, port)
    else:
        # asyncio.create_task 模式（需调用方已有事件循环）
        async def _start_async():
            await _observer_server.start()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_start_async())
            _log.info("Observer Server 已启动（任务模式）: ws://%s:%d", host, port)
        except RuntimeError:
            _log.error("无法启动 Observer Server：无运行中的事件循环，请使用 use_thread=True")
            _observer_server = None


def stop_observer_server() -> None:
    """停止 Observer Server。"""
    global _observer_server, _observer_thread, _observer_loop

    if _observer_server is None:
        return

    if _observer_loop is not None:
        # 线程模式：停止事件循环
        try:
            _observer_loop.call_soon_threadsafe(_observer_loop.stop)
        except Exception:
            pass

    _observer_server = None
    _observer_thread = None
    _observer_loop = None
    _log.info("Observer Server 已停止")


def get_observer_server():
    """获取 Observer Server 实例（供外部广播调用）。"""
    return _observer_server


def broadcast_observer(event: str, data: dict, channels: set[str] | None = None) -> int:
    """通过 Observer Server 广播事件（线程安全）。

    Args:
        event: 事件名称
        data: 事件数据
        channels: 目标频道集合（None 表示所有客户端）

    Returns:
        成功发送的客户端数量
    """
    if _observer_server is None or _observer_loop is None:
        return 0

    future = asyncio.run_coroutine_threadsafe(
        _observer_server.broadcast(event, data, channels),
        _observer_loop
    )
    try:
        return future.result(timeout=5.0)
    except Exception as e:
        _log.error("Observer 广播失败: %s", e)
        return 0


def shutdown_all() -> None:
    """关闭所有 WebSocket 服务（bridge + observer）。"""
    stop_ws_server()
    stop_observer_server()

    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False)
        _executor = None

    _log.info("所有 WebSocket 服务已关闭")
