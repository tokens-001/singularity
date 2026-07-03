"""观察者智能体 — 共享状态：队列、回调表、线程控制。

所有 _observer_*.py 模块通过导入此文件共享同一份状态实例，避免重复定义。
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable

_log = logging.getLogger("observer")

# 待处理的用户消息队列
_chat_queue: queue.Queue[tuple[str, str, Callable[[dict], None], str]] = queue.Queue()

# 已连接客户端的回复回调注册表
_pending_replies: dict[str, Callable[[dict], None]] = {}
_replies_lock = threading.Lock()

# 守护线程控制
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None

# 异常告警去重: key -> last_alert_timestamp
_alert_history: dict[str, float] = {}
_alert_lock = threading.Lock()
