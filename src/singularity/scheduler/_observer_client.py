"""观察者智能体 — 客户端管理：提交问题 + 异常检测 + 定义会话"""

from __future__ import annotations

import time
from collections.abc import Callable

from singularity.scheduler import config, witness
from singularity.scheduler._observer_shared import (
    _alert_history,
    _alert_lock,
    _chat_queue,
    _log,
    _pending_replies,
    _replies_lock,
)

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
    """检测应主动推送的异常事件。

    🔴 **每条都要带 `why`（为什么）+ `todo`（怎么办）**（2026-09-19 用户提）。
    原来只有一行事实（"任务 X 已停滞超过 10 分钟"），看告警的人还得自己回去翻代码才知道
    该干嘛 —— §77.5 那轮修的是**出口**（落盘 + 广播），这条修的是**内容**：
    只有事实的告警等于把排查工作原样退还给读的人。
    ⚠️ 写 `why` / `todo` 时**只写核过的机制** —— 告警会被人当依据用，编一句就把人带沟里。
    """
    alerts: list[dict] = []
    now = time.time()

    # 停滞任务
    try:
        stalled = witness.check_stalled()   # 用默认阈值，见 witness.STALLED_AFTER_S
        for tid in stalled:
            key = f"stalled:{tid}"
            with _alert_lock:
                last = _alert_history.get(key, 0)
            if now - last > 3600:
                alerts.append({
                    "kind": "stalled_task",
                    "task_id": tid,
                    # ⚠️ **分钟数从常量算，别手写** —— 这里原来硬编码"10 分钟"，
                    #    而 `STALLED_AFTER_S = TASK_DEADLINE_S + 300 = 1200s`（= 20 分钟）
                    #    ⇒ 文案比实际**少说了 10 分钟**（2026-09-19 核出来）。
                    "message": f"任务 {tid} 已停滞超过"
                               f" {int(round(witness.STALLED_AFTER_S / 60))} 分钟",
                    "why": "心跳是**每个 turn 写一次**（`_exec.run` 的 turn 循环开头）、"
                           "不是定时器 ⇒ 它陈旧**不等于** worker 死了；"
                           "但阈值比整个任务的死线还长（`TASK_DEADLINE_S + 300s`），"
                           "陈旧到这个程度说明这一轮早该结束了。",
                    "todo": f"先看 `.qidian/heartbeats/{tid}_*.json` 的 mtime 和 "
                            f"`.qidian/partial_usage/{tid}.json` 里的 `result_preview`："
                            f"还在变 = 在干活，别动；不动了就 `POST /api/tasks/{tid}/cancel`"
                            f"（观察者也能直接取消），再决定重试还是打捞产物。",
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
                        "why": "心跳文件是**每 (task, level) 一个**，任务到终态时由 "
                               "`witness._cleanup_terminal_heartbeat` 顺手清掉 "
                               "⇒ 积压 = 有一批任务**没走到那一步**：异常收尾、"
                               "进程被杀、或者清不掉。",
                        "todo": "看 `.qidian/heartbeats/` 里那些文件名对应的任务在 "
                                "`.qidian/tasks/` 下还在不在、状态是不是终态；"
                                "终态却还留着 = 清理漏了，可以直接删。",
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

