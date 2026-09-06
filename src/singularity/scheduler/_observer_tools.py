"""观察者智能体 — 工具注册表 + 工具实现函数"""
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

# 当前会话的执行模式（前端下拉硬传，绕开关键词检测的软链路）
_pending_exec_mode = "auto_edit"


def set_exec_mode(mode: str) -> None:
    """设置下一次创建任务的执行模式。auto_edit | confirm_changes。"""
    global _pending_exec_mode
    if mode in ("auto_edit", "confirm_changes"):
        _pending_exec_mode = mode

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

def _tool_get_system_status() -> dict[str, Any]:
    counts = witness._count_by_status()
    loads = witness._heartbeat_task_levels()
    pending_waits, done_durations = witness._timing_stats()
    token_totals = witness._token_stats()
    stalled = witness.check_stalled(timeout_seconds=600)
    return {
        "task_counts": counts,
        "running_by_level": loads,
        "running_total": sum(loads.values()),
        "avg_pending_wait_sec": round(sum(pending_waits) / len(pending_waits), 1) if pending_waits else 0,
        "avg_done_duration_sec": round(sum(done_durations) / len(done_durations), 1) if done_durations else 0,
        "token_totals": token_totals,
        "stalled_task_ids": stalled,
    }


def _tool_list_tasks(status: str | None = None, limit: int = 50,
                     project_id: str = "", active_only: bool = False) -> list[dict]:
    """列出任务。active_only=True 时只返回非终态 (排除 DONE/FAILED/ROLLED_BACK)。"""
    tasks: list[dict] = []
    terminal = {"done", "failed", "rolled_back"}
    for p in tracker.tasks_dir().glob("*.json"):
        try:
            t = tracker.Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
        if status and t.status.value != status:
            continue
        if active_only and t.status.value in terminal:
            continue
        if project_id and t.project_id != project_id:
            continue
        tasks.append(t.to_dict())
    tasks.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return tasks[:limit]


def _tool_get_task_details(task_id: str) -> dict[str, Any]:
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": f"task {task_id} not found"}

    trace: dict[str, Any] = {}
    trace_dir = getattr(config, "TRACE_DIR", None)
    if trace_dir:
        trace_path = trace_dir / f"{task_id}.json"
        if trace_path.exists():
            try:
                trace = json.loads(trace_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    return {"task": task.to_dict(), "trace": trace}


def _tool_list_stalled_tasks(timeout_seconds: float = 600) -> list[str]:
    return witness.check_stalled(timeout_seconds=timeout_seconds)


def _tool_get_judge_stats() -> dict[str, Any]:
    # ponytail: judge_monitor 已移除
    return {"models": {}, "anomalies": [], "note": "judge_monitor removed"}


def _tool_get_recent_events(limit: int = 20) -> list[dict]:
    traces: list[dict] = []
    trace_dir = getattr(config, "TRACE_DIR", None)
    if not trace_dir or not trace_dir.exists():
        return traces
    for p in sorted(trace_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            traces.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return traces


# ═══════════════════════════════════════════════════════════════
# 写操作工具
# ═══════════════════════════════════════════════════════════════

def _tool_create_task(description: str, level: str = "any") -> dict:
    """创建新任务,自动分类并启动调度循环。

    从用户描述中检测执行模式: "每一步确认/让我审"→confirm_changes, 否则 auto_edit。
    """
    try:
        task = tracker.create(description)
        # 执行模式：优先前端硬传的 _pending_exec_mode；用户描述含关键词时覆盖
        desc_lower = description.lower()
        if any(w in desc_lower for w in ("每一步确认", "让我审", "步步确认", "每步确认", "变更确认")):
            mode = "confirm_changes"
        else:
            mode = _pending_exec_mode
        # LLM 分类任务类型
        route_type = "default"
        try:
            from singularity.scheduler.router import route as classify_route
            r = classify_route(description)
            route_type = r.task_type
            gate = r.gate_required
        except Exception:
            gate = False
        tracker.transition(task.id, tracker.TaskStatus.PENDING, route_level=level,
                          route_locked=True, route_type=route_type, route_gate=gate)
        # 写 execution_mode 到 task
        t = tracker.read_task(task.id)
        if t:
            t.execution_mode = mode
            tracker._write(t)
        # 确保调度循环在跑
        try:
            import singularity.web.app as app_mod
            if not app_mod._loop_running:
                app_mod.start_loop(concurrent=2)
        except Exception:
            pass
        return {"ok": True, "task_id": task.id, "type": route_type, "description": description}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _tool_delete_task(task_id: str) -> dict:
    """删除指定任务（谨慎使用）。"""
    try:
        from singularity.scheduler import tracker
        t = tracker.read_task(task_id)
        if t is None:
            return {"ok": False, "error": f"任务 {task_id} 不存在"}
        p = tracker._path(task_id)
        if p.exists():
            p.unlink()
        return {"ok": True, "deleted": task_id, "description": t.description[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _tool_delete_failed_tasks() -> dict:
    """批量清除所有失败任务。"""
    try:
        from singularity.scheduler import tracker
        deleted = []
        for p in tracker.tasks_dir().glob("*.json"):
            try:
                import json
                d = json.loads(p.read_text())
                if d.get("status") == "failed":
                    deleted.append(d["id"][:8])
                    p.unlink()
            except Exception:
                pass
        return {"ok": True, "deleted_count": len(deleted), "deleted": deleted}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _tool_control_loop(action: str) -> dict:
    """控制调度循环：start/stop/status。"""
    try:
        import singularity.web.app as app_mod
        action = action.lower().strip()
        if action == "start":
            ok = app_mod.start_loop(concurrent=2)
            return {"ok": ok, "running": True, "message": "调度循环已启动"}
        elif action == "stop":
            ok = app_mod.stop_loop()
            return {"ok": ok, "running": app_mod._loop_running, "message": "调度循环已停止"}
        else:
            return {"ok": True, "running": app_mod._loop_running, "concurrent": app_mod._loop_concurrent}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _tool_list_projects() -> list[dict]:
    """列出所有项目及状态。"""
    from singularity.scheduler.project import list_all
    return [{"id": p.id, "name": p.name, "phase": p.phase, "task_count": len(p.task_ids)} for p in list_all()]


# ═══════════════════════════════════════════════════════════════
# OpenAI function calling 工具定义
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 工具注册表 — 加工具只需加一条, 自动生成 OpenAI schema + dispatch
# ═══════════════════════════════════════════════════════════════

_TOOL_REGISTRY: list[dict] = [
    # {name, description, handler, params: {param_name: {type, description, required?}}}
    {"name": "get_system_status", "description": "获取系统整体状态：任务计数、运行负载、平均等待/完成时间、token消耗、停滞任务列表。",
     "handler": _tool_get_system_status, "params": {}},
    {"name": "list_tasks", "description": "列出任务，可按状态过滤，默认按更新时间倒序。",
     "handler": _tool_list_tasks, "params": {
         "status": {"type": "string", "description": "过滤状态如 pending/running/done/failed"},
         "limit": {"type": "integer", "description": "最多返回条数，默认50"},
     }},
    {"name": "get_task_details", "description": "获取单个任务的完整字段和执行trace。",
     "handler": _tool_get_task_details, "params": {
         "task_id": {"type": "string", "description": "任务ID", "required": True},
     }},
    {"name": "list_stalled_tasks", "description": "列出停滞超过指定秒数的任务。",
     "handler": _tool_list_stalled_tasks, "params": {
         "timeout_seconds": {"type": "number", "description": "停滞阈值秒数，默认600"},
     }},
    {"name": "get_judge_stats", "description": "获取裁判统计：各任务类型通过率、模型偏差、异常事件、分数分布。",
     "handler": _tool_get_judge_stats, "params": {}},
    {"name": "get_recent_events", "description": "获取系统最近执行的事件列表。",
     "handler": _tool_get_recent_events, "params": {
         "limit": {"type": "integer", "description": "最多返回条数，默认20"},
     }},
    {"name": "create_task", "description": "创建待执行任务。用户说'帮我做xxx'时调用。",
     "handler": _tool_create_task, "params": {
         "description": {"type": "string", "description": "任务描述", "required": True},
         "level": {"type": "string", "description": "任务层级(留空默认any)"},
     }},
    {"name": "control_loop", "description": "控制调度循环：start启动/stop停止/status查看状态。",
     "handler": _tool_control_loop, "params": {
         "action": {"type": "string", "description": "start/stop/status", "required": True},
     }},
    {"name": "list_projects", "description": "列出所有项目及当前阶段。",
     "handler": _tool_list_projects, "params": {}},
    {"name": "delete_task", "description": "删除指定任务（不可恢复）。",
     "handler": _tool_delete_task, "params": {
         "task_id": {"type": "string", "description": "要删除的任务ID", "required": True},
     }},
    {"name": "delete_failed_tasks", "description": "批量清除所有失败状态的任务。",
     "handler": _tool_delete_failed_tasks, "params": {}},
]

def _build_openai_tools() -> list[dict]:
    """从 _TOOL_REGISTRY 自动生成 OpenAI function calling schema。"""
    tools = []
    for t in _TOOL_REGISTRY:
        props = {}
        required = []
        for pname, pinfo in t["params"].items():
            props[pname] = {"type": pinfo["type"], "description": pinfo.get("description", "")}
            if pinfo.get("required"):
                required.append(pname)
        schema: dict = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        tools.append({
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": schema},
        })
    return tools

OBSERVER_TOOLS = _build_openai_tools()
def _build_system_prompt() -> str:
    """从 _TOOL_REGISTRY 自动生成 prompt, 加工具自动出现。"""
    # ponytail: 工具列表不写进prompt, function calling的tools参数已包含完整schema, 省~600字符
    return """你是奇点,一个能直接干活的 AI 软件开发助手。用户找你是让你做事,不是聊天。

行为规则(严格遵守):
1. 用户说要做东西→必须调 create_task,别打招呼别问"需要什么帮助"
2. 只有纯闲聊(你好/谢谢/你是谁)才回文字,否则必须调工具干活
3. create_task 的 description 必须详细:功能清单+交互细节+视觉效果+技术栈+验收标准
4. 用户说"直接做/别问了/快做"→一句话不说,直接 create_task
5. 回复一句话说清做了什么事。创建任务后自动启动调度循环
6. 用户输入很短(<20字,如"做个番茄钟")→不要追问,直接基于常识补全详细 description 然后 create_task
7. 用户输入很长(有详细需求)→可简要确认关键点再 create_task,但最多一轮
8. 用户说"每一步确认/让我审/步步确认"→create_task 的 description 里包含"每一步确认"关键词,系统会自动切到变更确认模式"""

OBSERVER_SYSTEM_PROMPT = _build_system_prompt()

# ═══════════════════════════════════════════════════════════════
# Step 3: 定义层 4 角色 (Observer → 搞清楚用户要什么)
# ═══════════════════════════════════════════════════════════════

_OBSERVER_DEFINITION_ROLES: dict[str, dict] = {}

