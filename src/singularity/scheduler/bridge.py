"""bridge.py — WebSocket 桥接层 (T1 + T5)

与 SSE 共存: 同样的 _sse_clients 推送逻辑, WebSocket 客户端也接收。
协议: JSON-RPC 2.0 通知 (单向推送, 客户端通过 HTTP API 发送请求)
认证: X-Qidian-Token 请求头

T5: 集成 Observer Server，提供统一启动入口与调度事件钩子。
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
_observer_stop_signal: asyncio.Event | None = None

# 共享线程池（供 bridge 与 observer 复用）
_executor: ThreadPoolExecutor | None = None

# 启动状态标记
_all_started = False


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
            # ponytail: always use cross-thread path — Flask threads have no running loop
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
            await client.send_json({
                "jsonrpc": "2.0", "method": "auth_error",
                "params": {"message": "首条消息必须是 auth"}}
            )
            return

        async for _ in ws:
            pass
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
            await asyncio.Future()

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


# ── Observer Server 集成 (T5) ─────────────────────────────────────────────

def start_observer_server(
    host: str = "0.0.0.0",
    port: int = 8765,
    use_thread: bool = True,
) -> None:
    """启动 Observer Server（非阻塞）。

    Args:
        host: 监听地址
        port: 监听端口
        use_thread: True 使用独立线程，False 使用 asyncio.create_task
    """
    global _observer_server, _observer_thread, _observer_loop, _observer_stop_signal

    from singularity.observer.server import ObserverServer

    if _observer_server is not None:
        _log.warning("Observer Server 已在运行")
        return

    _observer_server = ObserverServer(host=host, port=port)
    _observer_stop_signal = asyncio.Event()

    if use_thread:
        async def _run_observer():
            await _observer_server.start()
            try:
                await _observer_stop_signal.wait()
            finally:
                await _observer_server.stop()

        def _thread_entry():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            global _observer_loop
            _observer_loop = loop
            loop.run_until_complete(_run_observer())

        _observer_thread = threading.Thread(
            target=_thread_entry,
            daemon=True,
            name="observer-server"
        )
        _observer_thread.start()
        _log.info("Observer Server 已启动（线程模式）: ws://%s:%d", host, port)
    else:
        async def _start_async():
            global _observer_loop
            _observer_loop = asyncio.get_running_loop()
            await _observer_server.start()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_start_async())
            _log.info("Observer Server 已启动（任务模式）: ws://%s:%d", host, port)
        except RuntimeError:
            _log.error("无法启动 Observer Server：无运行中的事件循环，请使用 use_thread=True")
            _observer_server = None
            _observer_stop_signal = None


def stop_observer_server() -> None:
    """停止 Observer Server，优雅关闭。"""
    global _observer_server, _observer_thread, _observer_loop, _observer_stop_signal

    if _observer_server is None:
        return

    if _observer_stop_signal is not None and _observer_loop is not None and not _observer_loop.is_closed():
        try:
            _observer_loop.call_soon_threadsafe(_observer_stop_signal.set)
        except Exception:
            pass

    if _observer_loop is not None and not _observer_loop.is_closed():
        async def _explicit_stop():
            try:
                await _observer_server.stop()
            except Exception:
                pass

        try:
            future = asyncio.run_coroutine_threadsafe(_explicit_stop(), _observer_loop)
            future.result(timeout=5.0)
        except Exception:
            pass

    if _observer_loop is not None and not _observer_loop.is_closed():
        try:
            _observer_loop.call_soon_threadsafe(_observer_loop.stop)
        except Exception:
            pass

    if _observer_thread is not None and _observer_thread.is_alive():
        _observer_thread.join(timeout=5.0)
        if _observer_thread.is_alive():
            _log.warning("Observer Server 线程未能在超时内退出")

    _observer_server = None
    _observer_thread = None
    _observer_loop = None
    _observer_stop_signal = None
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


# ── T5: 统一启动入口与调度事件钩子 ─────────────────────────────────────────

def start_all(
    ws_host: str = "127.0.0.1",
    ws_port: int = 5051,
    observer_host: str = "0.0.0.0",
    observer_port: int = 8765,
) -> None:
    """统一启动所有 WebSocket 服务（bridge WS + Observer）。

    供 scheduler loop / 应用入口一键调用。
    """
    global _all_started
    if _all_started:
        _log.warning("bridge.start_all: 服务已在运行，跳过")
        return

    _log.info("bridge.start_all: 启动 WebSocket 桥接与 Observer 服务")

    # 1) 启动 bridge WS（JSON-RPC 认证通道）
    try:
        start_ws_server(host=ws_host, port=ws_port)
    except Exception as e:
        _log.error("bridge WS 启动失败: %s", e)

    # 2) 启动 Observer WS（实时事件推送通道）
    try:
        start_observer_server(host=observer_host, port=observer_port)
    except Exception as e:
        _log.error("Observer Server 启动失败: %s", e)

    _all_started = True
    _log.info(
        "bridge.start_all: 完成 — bridge=ws://%s:%d  observer=ws://%s:%d",
        ws_host, ws_port, observer_host, observer_port,
    )


def stop_all() -> None:
    """统一停止所有 WebSocket 服务。shutdown_all 的别名。"""
    shutdown_all()


def shutdown_all() -> None:
    """关闭所有 WebSocket 服务（bridge + observer）。"""
    global _all_started
    stop_ws_server()
    stop_observer_server()

    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False)
        _executor = None

    _all_started = False
    _log.info("所有 WebSocket 服务已关闭")


# ── 调度事件钩子 ──────────────────────────────────────────────────────────

def emit_task_event(event: str, task_id: str, data: dict | None = None) -> int:
    """发送任务生命周期事件到 Observer。

    支持的 event: task_queued, task_started, task_done, task_failed, task_blocked

    Args:
        event: 事件名称
        task_id: 任务 ID
        data: 附加数据（可选）

    Returns:
        成功推送的客户端数
    """
    payload = {"task_id": task_id, "ts": time.time()}
    if data:
        payload.update(data)
    return broadcast_observer(event, payload, channels={"tasks"})


def emit_system_event(event: str, data: dict | None = None) -> int:
    """发送系统级事件到 Observer。

    Args:
        event: 事件名称（如 loop_idle, loop_drain, scheduler_start）
        data: 附加数据

    Returns:
        成功推送的客户端数
    """
    payload = {"ts": time.time()}
    if data:
        payload.update(data)
    return broadcast_observer(event, payload, channels={"system"})


def emit_metrics_event(data: dict) -> int:
    """发送指标采样事件到 Observer。

    Args:
        data: 指标数据字典

    Returns:
        成功推送的客户端数
    """
    data["ts"] = time.time()
    return broadcast_observer("metrics", data, channels={"metrics"})


def is_observer_running() -> bool:
    """Observer Server 是否正在运行。"""
    return _observer_server is not None and _observer_thread is not None


def is_ws_running() -> bool:
    """Bridge WS Server 是否正在运行。"""
    return _WS_THREAD is not None and _WS_THREAD.is_alive()


def status() -> dict:
    """返回所有 WebSocket 服务的运行状态。"""
    return {
        "bridge_ws": {
            "running": is_ws_running(),
            "clients": len(_ws_clients),
        },
        "observer": {
            "running": is_observer_running(),
            "clients": _observer_server.client_count if _observer_server else 0,
        },
    }