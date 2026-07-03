"""观察者智能体 — Worker 线程 + 启动/停止"""

from __future__ import annotations

import logging
import queue
import threading
import time

from singularity.scheduler._observer_shared import _log, _chat_queue, _stop_event, _worker_thread, _pending_replies, _replies_lock
from singularity.scheduler._observer_client import _check_anomalies
from singularity.scheduler._observer_answer import _answer_question


# ═══════════════════════════════════════════════════════════════
# 只读查询工具（纯 Python 函数，直接读取现有数据）
# ═══════════════════════════════════════════════════════════════


def _observer_worker() -> None:
    _log.info("Observer agent worker started")
    while not _stop_event.is_set():
        try:
            # 1. 处理聊天消息
            while not _chat_queue.empty():
                try:
                    item = _chat_queue.get_nowait()
                    if len(item) == 4:
                        client_id, question, reply_callback, project_id = item
                    else:
                        client_id, question, reply_callback = item
                        project_id = ""
                except queue.Empty:
                    break
                # 如果没有注册 callback，用入参 callback
                if client_id:
                    with _replies_lock:
                        _pending_replies[client_id] = reply_callback
                answer = _answer_question(question, project_id=project_id)
                payload = {
                    "jsonrpc": "2.0",
                    "method": "observer_chat",
                    "params": {"type": "answer", "text": answer, "ts": time.time()},
                }
                reply_callback(payload)

            # 2. 主动异常检测
            for alert in _check_anomalies():
                payload = {
                    "jsonrpc": "2.0",
                    "method": "observer_alert",
                    "params": alert,
                }
                # 广播给所有已注册客户端
                with _replies_lock:
                    callbacks = list(_pending_replies.values())
                for callback in callbacks:
                    try:
                        callback(payload)
                    except Exception:
                        _log.warning("广播告警失败", exc_info=True)

        except Exception:
            _log.exception("observer worker loop error")

        # 使用 wait 代替 sleep，便于立即响应 stop
        _stop_event.wait(5.0)

    _log.info("Observer agent worker stopped")


def start_observer() -> None:
    """启动观察者智能体守护线程。"""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        _log.warning("Observer agent already running")
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_observer_worker, name="observer-agent", daemon=True)
    _worker_thread.start()
    _log.info("Observer agent started")


def stop_observer() -> None:
    """停止观察者智能体守护线程。"""
    global _worker_thread
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5.0)
        if _worker_thread.is_alive():
            _log.warning("Observer agent thread did not stop in time")
        _worker_thread = None
    _log.info("Observer agent stopped")


def is_running() -> bool:
    return _worker_thread is not None and _worker_thread.is_alive()

