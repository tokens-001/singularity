"""observer_agent.py — 观察者智能体

旁路守护线程，通过只读工具查询系统状态并回答用户自然语言问题。
不修改 scheduler / dispatcher / executor 的任何执行逻辑。

Step 3: 支持定义层4角色 (产品经理/交互设计师/UI设计师/研究员)。
Observer 负责搞清楚用户要什么，不做设计决策。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from singularity.scheduler import config, tracker, witness

_log = logging.getLogger("observer")

# 待处理的用户消息队列：元素为 (client_id, question, reply_callback)
_chat_queue: queue.Queue[tuple[str, str, Callable[[dict], None]]] = queue.Queue()

# 已连接客户端的回复回调注册表
_pending_replies: dict[str, Callable[[dict], None]] = {}
_replies_lock = threading.Lock()

# 守护线程控制
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None

# 异常告警去重：key -> last_alert_timestamp
_alert_history: dict[str, float] = {}
_alert_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
# 只读查询工具（纯 Python 函数，直接读取现有数据）
# ═══════════════════════════════════════════════════════════════


def submit_question(client_id: str, question: str, reply_callback: Callable[[dict], None],
                    project_id: str = "") -> None:
    """将用户问题提交给观察者队列。"""
    _chat_queue.put((client_id, question, reply_callback, project_id))


def register_client(client_id: str, reply_callback: Callable[[dict], None]) -> None:
    """注册客户端回复通道。"""
    with _replies_lock:
        _pending_replies[client_id] = reply_callback


def unregister_client(client_id: str) -> None:
    """注销客户端回复通道。"""
    with _replies_lock:
        _pending_replies.pop(client_id, None)


def _send_to_client(client_id: str, payload: dict) -> None:
    with _replies_lock:
        callback = _pending_replies.get(client_id)
    if callback:
        try:
            callback(payload)
        except Exception:
            _log.warning("发送消息到客户端 %s 失败", client_id, exc_info=True)


# ═══════════════════════════════════════════════════════════════
# 异常主动检测
# ═══════════════════════════════════════════════════════════════

def _check_anomalies() -> list[dict]:
    """检测应主动推送的异常事件。"""
    alerts: list[dict] = []
    now = time.time()

    # 停滞任务
    try:
        stalled = witness.check_stalled(timeout_seconds=600)
        for tid in stalled:
            key = f"stalled:{tid}"
            with _alert_lock:
                last = _alert_history.get(key, 0)
            if now - last > 3600:
                alerts.append({
                    "kind": "stalled_task",
                    "task_id": tid,
                    "message": f"任务 {tid} 已停滞超过 10 分钟",
                    "ts": now,
                })
                with _alert_lock:
                    _alert_history[key] = now
    except Exception:
        _log.exception("stalled check failed")

    # ponytail: judge_monitor 已移除，裁判异常检查不再需要

    # 心跳文件积压（超过 200 个）
    try:
        hb_dir = config.QIDIAN_DIR / "heartbeats"
        if hb_dir.exists():
            count = len(list(hb_dir.glob("*.json")))
            if count > 200:
                key = "heartbeat_backlog"
                with _alert_lock:
                    last = _alert_history.get(key, 0)
                if now - last > 3600:
                    alerts.append({
                        "kind": "heartbeat_backlog",
                        "message": f"心跳文件积压：{count} 个",
                        "ts": now,
                    })
                    with _alert_lock:
                        _alert_history[key] = now
    except Exception:
        _log.exception("heartbeat backlog check failed")

    return alerts


# ═══════════════════════════════════════════════════════════════
# 守护线程主循环
# ═══════════════════════════════════════════════════════════════

