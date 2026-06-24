"""observer_agent.py — 观察者智能体

旁路守护线程，通过只读工具查询系统状态并回答用户自然语言问题。
不修改 scheduler / dispatcher / executor 的任何执行逻辑。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from typing import Any, Callable

import httpx

from singularity.scheduler import config, tracker, witness
from singularity.scheduler import judge_monitor

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


def _tool_list_tasks(status: str | None = None, limit: int = 50) -> list[dict]:
    tasks: list[dict] = []
    for p in tracker.tasks_dir().glob("*.json"):
        try:
            t = tracker.Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
        if status and t.status.value != status:
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
    store = judge_monitor.JudgeMonitorStore()
    qidian_dir = getattr(config, "QIDIAN_DIR", None)
    if qidian_dir:
        path = qidian_dir / "judge_monitor.json"
        if path.exists():
            store.load(path)
    return store.get_stats()


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

def _tool_create_task(description: str, level: str = "E") -> dict:
    """创建新任务。"""
    try:
        task = tracker.create(description)
        if level:
            tracker.transition(task.id, tracker.TaskStatus.PENDING, route_level=level, route_locked=True)
        return {"ok": True, "task_id": task.id, "level": level, "description": description}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _tool_control_loop(action: str) -> dict:
    """控制调度循环：start/stop/status。"""
    try:
        from singularity.web.app import start_loop, stop_loop, _loop_running, _loop_concurrent
        action = action.lower().strip()
        if action == "start":
            ok = start_loop(concurrent=2)
            return {"ok": ok, "running": True, "message": "调度循环已启动"}
        elif action == "stop":
            ok = stop_loop()
            return {"ok": ok, "running": _loop_running, "message": "调度循环已停止"}
        else:
            return {"ok": True, "running": _loop_running, "concurrent": _loop_concurrent}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _tool_list_projects() -> list[dict]:
    """列出所有项目及状态。"""
    from singularity.scheduler.project import list_all
    return [{"id": p.id, "name": p.name, "phase": p.phase, "task_count": len(p.task_ids)} for p in list_all()]


# ═══════════════════════════════════════════════════════════════
# OpenAI function calling 工具定义
# ═══════════════════════════════════════════════════════════════

OBSERVER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": "获取系统整体状态：任务计数、各层运行负载、平均等待/完成时间、token消耗、停滞任务列表。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "列出任务，可按状态过滤，默认按更新时间倒序。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "过滤状态如 pending/dispatched/running/done/failed"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认50"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_details",
            "description": "获取单个任务的完整字段和执行trace。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务ID"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_stalled_tasks",
            "description": "列出停滞超过指定秒数的任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timeout_seconds": {"type": "number", "description": "停滞阈值秒数，默认600"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_judge_stats",
            "description": "获取裁判统计：各任务类型通过率、模型偏差、异常事件、分数分布、总判定数。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_events",
            "description": "获取最近N条执行trace事件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回条数，默认20"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "创建新任务。用户说'帮我做个xxx'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "任务描述"},
                    "level": {"type": "string", "description": "任务层级：E/E+/D，默认E"},
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_loop",
            "description": "控制调度循环：start启动/stop停止/status查看状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "start/stop/status"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "列出所有项目及当前阶段。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

OBSERVER_SYSTEM_PROMPT = """你是 Singularity Dispatch 的主交互智能体，用户通过你管理系统的一切。

可用工具：
查询类（只读）：
- get_system_status: 系统整体状态
- list_tasks: 任务列表，可按status/limit过滤
- get_task_details: 单个任务详情+执行trace
- list_stalled_tasks: 停滞任务
- get_judge_stats: 裁判统计与异常
- get_recent_events: 最近执行事件
- list_projects: 项目列表及阶段

操作类（写）：
- create_task: 创建新任务。用户说"帮我做xxx"时调用
- control_loop: 启动/停止调度循环

回答要求：
1. 简洁、准确，使用中文
2. 数据必须来自工具返回，不编造
3. 异常时给出原因和建议
4. 用户要求操作时主动执行（创建任务、控制循环等）
5. 执行操作后报告结果
"""


# ═══════════════════════════════════════════════════════════════
# 工具执行分发
# ═══════════════════════════════════════════════════════════════

def _execute_observer_tool(name: str, args: dict[str, Any]) -> str:
    try:
        if name == "get_system_status":
            result = _tool_get_system_status()
        elif name == "list_tasks":
            result = _tool_list_tasks(**args)
        elif name == "get_task_details":
            result = _tool_get_task_details(**args)
        elif name == "list_stalled_tasks":
            result = _tool_list_stalled_tasks(**args)
        elif name == "get_judge_stats":
            result = _tool_get_judge_stats()
        elif name == "get_recent_events":
            result = _tool_get_recent_events(**args)
        elif name == "create_task":
            result = _tool_create_task(**args)
        elif name == "control_loop":
            result = _tool_control_loop(**args)
        elif name == "list_projects":
            result = _tool_list_projects()
        else:
            result = {"error": f"unknown tool {name}"}
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ═══════════════════════════════════════════════════════════════
# LLM 推理（直接调用 OpenAI 兼容 API，避免依赖内部执行器细节）
# ═══════════════════════════════════════════════════════════════

def _get_observer_cfg() -> dict[str, Any]:
    """优先复用配置中 observer / E 模型，否则默认走本地 Ollama。"""
    agents = getattr(config, "AGENTS", {}) or {}
    observer_cfg = agents.get("observer") or agents.get("E") or {}
    if observer_cfg.get("model"):
        return {
            "model": observer_cfg.get("model"),
            "api_key": observer_cfg.get("api_key", ""),
            "base_url": observer_cfg.get("base_url", "https://api.deepseek.com/v1"),
            "temperature": observer_cfg.get("temperature", 0.3),
            "max_tokens": observer_cfg.get("max_tokens", 1024),
        }
    # ponytail: 默认走 DeepSeek（已配置 DEEPSEEK_API_KEY）
    return {
        "model": "deepseek-chat",
        "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "base_url": "https://api.deepseek.com/v1",
        "temperature": 0.3,
        "max_tokens": 1024,
    }


def _build_status_context() -> str:
    """预取全量系统状态，注入 prompt，无需 function calling。"""
    status = _tool_get_system_status()
    tasks = _tool_list_tasks(limit=20)
    stalled = _tool_list_stalled_tasks()
    judge = _tool_get_judge_stats()
    recent = _tool_get_recent_events(limit=10)
    return json.dumps({
        "系统状态": status,
        "最近任务": tasks,
        "卡住任务": stalled,
        "裁判统计": judge,
        "最近事件": recent,
    }, ensure_ascii=False, indent=2)


DIRECT_SYSTEM_PROMPT = """你是 Singularity Dispatch 的主交互智能体。下面是当前系统的实时状态数据。根据这些数据回答用户问题，用户可以要求你创建任务或控制调度循环。

规则：
1. 基于提供的数据回答，不编造
2. 简洁准确，使用中文
3. 异常情况给出原因和建议
4. 数据中没有的信息，诚实说不知道
5. 用户要求创建任务时，引导他们使用完整功能模式"""


def _answer_question(question: str) -> str:
    cfg = _get_observer_cfg()
    api_key = cfg.get("api_key", "")
    base_url = cfg.get("base_url", "https://api.deepseek.com/v1").rstrip("/")
    model = cfg.get("model", "deepseek-chat")

    # ponytail: Ollama 本地模型默认无 api_key，用 direct 模式
    use_direct = not api_key or "ollama" in base_url or "localhost" in base_url

    if use_direct:
        # Direct 模式：预取状态注入 prompt，一次调用出结果
        try:
            ctx = _build_status_context()
        except Exception as e:
            ctx = f"（状态获取失败：{e}）"
        system = DIRECT_SYSTEM_PROMPT + "\n\n## 当前系统状态\n```json\n" + ctx + "\n```"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ]
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": cfg.get("temperature", 0.3),
            "max_tokens": cfg.get("max_tokens", 1024),
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(f"{base_url}/chat/completions", headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content.strip() if content else "（模型返回空内容）"
        except Exception as e:
            return f"调用 LLM 失败：{e}"

    # Function calling 模式（有 API key 的云端模型）
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    messages = [
        {"role": "system", "content": OBSERVER_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    max_turns = 5
    with httpx.Client(timeout=60.0) as client:
        for _ in range(max_turns):
            try:
                resp = client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": messages,
                        "tools": OBSERVER_TOOLS,
                        "tool_choice": "auto",
                        "temperature": cfg.get("temperature", 0.3),
                        "max_tokens": cfg.get("max_tokens", 1024),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                return f"调用 LLM 失败：{e}"

            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})

            tool_calls = message.get("tool_calls") or []
            content = message.get("content")

            # ponytail: 如果只有文本没有工具调用，直接返回
            if content and not tool_calls:
                return content.strip()

            if not tool_calls:
                # 纯文本回答
                return content.strip() if content else "（模型未返回有效内容）"

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    args = {}
                tool_result = _execute_observer_tool(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_result,
                })

    return "（工具调用轮次耗尽，未能生成回答）"


# ═══════════════════════════════════════════════════════════════
# 公共 API：接收用户问题、返回回复回调
# ═══════════════════════════════════════════════════════════════

def submit_question(client_id: str, question: str, reply_callback: Callable[[dict], None]) -> None:
    """将用户问题提交给观察者队列。"""
    _chat_queue.put((client_id, question, reply_callback))


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

    # 裁判异常
    try:
        store = judge_monitor.JudgeMonitorStore()
        qidian_dir = getattr(config, "QIDIAN_DIR", None)
        if qidian_dir:
            path = qidian_dir / "judge_monitor.json"
            if path.exists():
                store.load(path)
        stats = store.get_stats()
        for anomaly in stats.get("anomalies", []):
            kind = anomaly.get("kind", "")
            detail = anomaly.get("detail", "")
            key = f"judge:{kind}:{detail}"
            with _alert_lock:
                last = _alert_history.get(key, 0)
            if now - last > 3600:
                alerts.append({
                    "kind": f"judge_anomaly:{kind}",
                    "message": detail,
                    "ts": now,
                })
                with _alert_lock:
                    _alert_history[key] = now
    except Exception:
        _log.exception("judge anomaly check failed")

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

def _observer_worker() -> None:
    _log.info("Observer agent worker started")
    while not _stop_event.is_set():
        try:
            # 1. 处理聊天消息
            while not _chat_queue.empty():
                try:
                    client_id, question, reply_callback = _chat_queue.get_nowait()
                except queue.Empty:
                    break
                # 如果没有注册 callback，用入参 callback
                if client_id:
                    with _replies_lock:
                        _pending_replies[client_id] = reply_callback
                answer = _answer_question(question)
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
