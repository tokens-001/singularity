"""scheduler → 宿主（web/CLI）的能力注册点。

scheduler 不该 import web —— 那是分层倒挂，还制造 import 环
（web.app ↔ scheduler.project / _observer_tools）。但有些能力只有宿主有：
启动调度循环、实时广播 SSE。宿主启动时在这里注册回调，scheduler 只调本模块。

未注册时全部降级为保守默认值，所以 scheduler 能脱离 web 独立跑（CLI/无头）。
"""

from __future__ import annotations

from typing import Callable

_loop_start: Callable[[int], bool] | None = None
_loop_stop: Callable[[], bool] | None = None
_loop_status: Callable[[], dict] | None = None
_event_sink: Callable[..., None] | None = None


def register_loop(start: Callable[[int], bool], stop: Callable[[], bool],
                  status: Callable[[], dict]) -> None:
    """宿主注册调度循环的启动/停止/查询。"""
    global _loop_start, _loop_stop, _loop_status
    _loop_start, _loop_stop, _loop_status = start, stop, status


def register_event_sink(sink: Callable[..., None]) -> None:
    """宿主注册 SSE 广播函数，签名同 app._sse_broadcast(kind, msg, ts=None, extra=None)。"""
    global _event_sink
    _event_sink = sink


def start_loop(concurrent: int = 1) -> bool:
    return _loop_start(concurrent) if _loop_start else False


def stop_loop() -> bool:
    return _loop_stop() if _loop_stop else False


def loop_status() -> dict:
    return _loop_status() if _loop_status else {"running": False, "concurrent": 0}


def emit(kind: str, msg: str, ts: float | None = None, extra: dict | None = None) -> None:
    """广播一个事件到前端。没注册 sink 时静默丢弃。"""
    if _event_sink:
        _event_sink(kind, msg, ts, extra)
