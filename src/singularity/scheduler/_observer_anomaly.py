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

_DEFINITION_ROLE_ORDER = ["product-manager", "interaction-designer", "ui-designer", "researcher"]
_DEFINITION_DOC_KEYS = {
    "product-manager": "prd",
    "interaction-designer": "interaction",
    "ui-designer": "ui_direction",
    "researcher": "research",
}


def _get_definition_session(project_id: str) -> dict:
    """获取或创建定义阶段会话状态。"""
    if project_id not in _DEFINITION_SESSIONS:
        _DEFINITION_SESSIONS[project_id] = {
            "active_role": "product-manager",
            "completed_roles": [],
            "history": [],  # [{role, content}]
            "phase": "defining",  # defining | gate1_waiting | done
        }
    return _DEFINITION_SESSIONS[project_id]


def _save_definition_artifact(project_id: str, role: str, content: str) -> None:
    """保存角色产出到项目目录。"""
    try:
        from singularity.scheduler.project import get_project_dir
        proj_dir = get_project_dir(project_id)
        doc_key = _DEFINITION_DOC_KEYS.get(role, role)
        doc = {"role": role, "content": content, "saved_at": time.time()}
        (proj_dir / f"{doc_key}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        _log.warning("保存定义文档失败: %s/%s", project_id, role)


def _advance_definition_role(project_id: str, session: dict) -> str | None:
    """推进到下一个定义角色。返回下一个角色 key，全部完成返回 None。"""
    current = session.get("active_role", "")
    if current and current not in session.get("completed_roles", []):
        session["completed_roles"].append(current)
    # 找下一个未完成的角色
    for role in _DEFINITION_ROLE_ORDER:
        if role not in session["completed_roles"]:
            session["active_role"] = role
            return role
    # 全部完成
    session["phase"] = "gate1_waiting"
    return None


def _build_definition_prompt(project_id: str, question: str) -> tuple[str, bool]:
    """构建定义阶段的多轮对话 prompt。返回 (system_prompt, gate1_ready)。"""
    session = _get_definition_session(project_id)
    role = session["active_role"]
    role_prompt = _definition_role_prompt(role)

    # 加载已完成角色的产出作为上下文
    completed_docs = []
    try:
        from singularity.scheduler.project import get_project_dir
        proj_dir = get_project_dir(project_id)
        for completed_role in session.get("completed_roles", []):
            doc_key = _DEFINITION_DOC_KEYS.get(completed_role, completed_role)
            doc_path = proj_dir / f"{doc_key}.json"
            if doc_path.exists():
                doc = json.loads(doc_path.read_text(encoding="utf-8"))
                completed_docs.append(f"## {completed_role} 产出\n{doc.get('content', '')[:1000]}")
    except Exception:
        pass

    context = "\n\n".join(completed_docs) if completed_docs else "（尚无上游产出）"

    # 注入历史消息 (最近3轮)
    history_text = ""
    for h in session.get("history", [])[-3:]:
        history_text += f"\n[{h.get('role','?')}]: {h.get('content','')[:300]}"

    system = f"""{role_prompt}

## 当前角色: {role}
## 已完成角色: {', '.join(session['completed_roles']) or '无'}
## 上游产出:
{context}
## 对话历史:
{history_text or '（新对话）'}

## 规则
- 你是定义层的 {role}，只做本角色职责范围内的事
- 产出必须是结构化 JSON (```json 包裹)
- 产出完成后,在回复末尾标注 [COMPLETE]
- 不要做设计决策,给选项让用户选
- 信息不足时追问,不编造"""

    gate1_ready = session.get("phase") == "gate1_waiting"
    return system, gate1_ready


# ═══════════════════════════════════════════════════════════════
# 公共 API：接收用户问题、返回回复回调
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

