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
    """创建新任务。"""
    try:
        task = tracker.create(description)
        if level:
            tracker.transition(task.id, tracker.TaskStatus.PENDING, route_level=level, route_locked=True)
        return {"ok": True, "task_id": task.id, "level": level, "description": description}
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
                    "level": {"type": "string", "description": "任务层级(两档后统一 any，留空即可)"},
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
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": "删除指定任务（不可恢复）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要删除的任务ID"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_failed_tasks",
            "description": "批量清除所有失败状态的任务。",
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
- delete_task: 删除指定任务
- delete_failed_tasks: 批量清除所有失败任务

回答要求：
1. 简洁、准确，使用中文
2. 数据必须来自工具返回，不编造
3. 异常时给出原因和建议
4. 用户要求操作时主动执行（创建任务、控制循环等）
5. 执行操作后报告结果
"""

# ═══════════════════════════════════════════════════════════════
# Step 3: 定义层 4 角色 (Observer → 搞清楚用户要什么)
# ═══════════════════════════════════════════════════════════════

_OBSERVER_DEFINITION_ROLES: dict[str, dict] = {}

def _load_observer_skills() -> dict[str, dict]:
    """加载 observer 下的 4 个定义层角色 skill。"""
    import re
    skills_dir = Path(__file__).resolve().parent.parent / "skills" / "observer"
    roles = {}
    for d in skills_dir.iterdir() if skills_dir.exists() else []:
        if not d.is_dir():
            continue
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            continue
        text = skill_md.read_text(encoding="utf-8")
        # 解析 frontmatter
        fm = {}
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().split("\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        fm[k.strip()] = v.strip()
                body = parts[2].strip()
            else:
                body = text
        else:
            body = text
        roles[fm.get("name", d.name)] = {
            "key": d.name,
            "name": fm.get("description", fm.get("name", d.name)),
            "system_prompt": body,
        }
    return roles


def _definition_role_prompt(role_key: str) -> str:
    """获取定义层角色 prompt。"""
    global _OBSERVER_DEFINITION_ROLES
    if not _OBSERVER_DEFINITION_ROLES:
        _OBSERVER_DEFINITION_ROLES = _load_observer_skills()
    # 精确匹配
    role = _OBSERVER_DEFINITION_ROLES.get(role_key, {})
    if role:
        return role.get("system_prompt", "")
    # 模糊匹配: 按 key 后缀
    for k, v in _OBSERVER_DEFINITION_ROLES.items():
        if k.endswith(role_key) or role_key in k:
            return v.get("system_prompt", "")
    return ""


DEFINITION_SYSTEM_PROMPT = """你是 Singularity 的定义层对话智能体。你的职责是搞清楚用户要什么，产出结构化文档交人确认（GATE1）。

你有 4 个角色帽子，根据对话阶段切换：

1. **产品经理** (product-manager) — 用户刚描述想法时启动
   问需求、出 PRD（功能/范围/竞品/成功标准）
   触发: 用户说"我要做xxx" "帮我设计xxx" "做一个xxx产品"

2. **交互设计师** (interaction-designer) — PRD 确认后启动
   设计用户流程、信息架构、页面结构、状态流转
   触发: 用户确认了 PRD

3. **UI 设计师** (ui-designer) — 与交互设计并行
   收集视觉偏好、风格参考、品牌调性，给出方向建议
   触发: 用户开始描述喜欢的风格/参考

4. **研究员** (researcher) — PRD 出来后并行启动
   市场调研、技术调研、可行性分析
   触发: PRD 确认后自动启动，或用户要求调研

工作流:
1. 用户说想法 → 自动切换到产品经理角色，问清楚需求，写 PRD
2. PRD 完成 → 提示用户在 GATE1 确认
3. 用户确认 → 同时启动 交互/UI/研究员（并行），产出 3 份文档
4. 4 份文档齐全 → 提示用户可以进入 GATE1，通过后开始架构设计

规则:
- 不问技术选型问题（那是架构师的事）
- 不做设计决策，给选项让用户选
- 每个角色输出必须是结构化 JSON
- 信息不足时追问，不编造
- GATE1 必须人确认，不自作主张通过

## Observer 调度输出 schema

每次角色切换或阶段推进时，先输出调度决策 JSON（```json 包裹），再输出角色文本:

{
  "role_switch": {
    "current": "product-manager|interaction-designer|ui-designer|researcher|none",
    "trigger": "auto_upstream_done|user_explicit|fallback",
    "carried_context_ref": "上游产出文件引用",
    "next_action": "ask|present|wait_user"
  },
  "gate_summary": {
    "artifacts": ["prd.json", "interaction.json", "ui_guidelines.json", "research.json"],
    "highlights": "本次产出的要点摘要",
    "pending_decision": "需要人确认的点"
  }
}

切换协议:
- auto_upstream_done: 上一个角色产出完成自动切下一个
- user_explicit: 用户显式要求换角色时切换
- fallback: 当前角色无法回答时回退到产品经理
- 用户可随时说"换到XX角色"手动切换"""

# ═══════════════════════════════════════════════════════════════
# Observer verdict_rollup schema (GATE3 — D4 + D3 配合)
# ═══════════════════════════════════════════════════════════════

OBSERVER_VERDICT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "observer_verdict_rollup",
        "description": "GATE3 验收汇总: 按 QA 报告的 fix_route 分级路由, Observer 汇总裁定",
        "schema": {
            "type": "object",
            "properties": {
                "qa_summary": {"type": "string", "description": "QA 报告摘要"},
                "security_summary": {"type": "string", "description": "安全审计摘要"},
                "fix_route_decision": {
                    "type": "string",
                    "enum": ["impl", "design", "note"],
                    "description": "最终路由: impl→回实现层修复, design→回GATE2重规划, note→记录不阻断"
                },
                "overall": {
                    "type": "string",
                    "enum": ["go", "no_go", "needs_human"],
                    "description": "go→自动进交付, no_go→打回, needs_human→升GATE3人审"
                }
            },
            "required": ["qa_summary", "fix_route_decision", "overall"]
        }
    }
}


def _get_definition_context(role_key: str = "", include_verdict_schema: bool = False) -> str:
    """构建定义层角色上下文。GATE3 时注入 verdict schema。"""
    prompt = DEFINITION_SYSTEM_PROMPT
    if include_verdict_schema:
        prompt += "\n\n## GATE3 验收汇总 schema\n你必须输出:\n```json\n" + \
            json.dumps(OBSERVER_VERDICT_SCHEMA["json_schema"]["schema"], ensure_ascii=False, indent=2) + \
            "\n```"
    if role_key:
        role_prompt = _definition_role_prompt(role_key)
        if role_prompt:
            prompt = role_prompt + "\n\n---\n\n" + prompt
    return prompt


def _any_project_at_gate3() -> bool:
    """检查是否有项目处于 GATE3 阶段 (需要 verdict schema)。"""
    try:
        from singularity.scheduler.project import list_all, Phase
        return any(p.phase == Phase.GATE3 for p in list_all())
    except Exception:
        return False


def _detect_definition_intent(question: str) -> str:
    """检测用户意图是否为定义层需求。返回角色 key 或空字符串。"""
    q = question.lower()
    # 产品/项目定义意图
    pm_triggers = ["我要做", "帮我设计", "做一个", "开发一个", "新产品", "新项目",
                   "立项", "prd", "产品方案", "需求分析", "功能设计"]
    if any(t in q for t in pm_triggers):
        return "product-manager"
    # UI/视觉意图
    ui_triggers = ["风格", "颜色", "好看", "设计感", "ui", "界面", "视觉", "品牌",
                   "参考图", "暗色", "亮色", "极简", "华丽"]
    if any(t in q for t in ui_triggers):
        return "ui-designer"
    # 交互意图
    ix_triggers = ["流程", "页面", "导航", "交互", "操作步骤", "用户体验", "ux",
                   "信息架构", "跳转"]
    if any(t in q for t in ix_triggers):
        return "interaction-designer"
    # 调研意图
    rs_triggers = ["调研", "竞品", "市场", "可行性", "有哪些", "对比", "参考方案"]
    if any(t in q for t in rs_triggers):
        return "researcher"
    return ""


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
        elif name == "delete_task":
            result = _tool_delete_task(**args)
        elif name == "delete_failed_tasks":
            result = _tool_delete_failed_tasks()
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
    observer_cfg = agents.get("observer") or agents.get("any") or {}
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

    # Step 3: 检测定义层意图，注入角色 prompt
    def_role = _detect_definition_intent(question)
    # D4: 检查是否有项目处于 GATE3, 注入 verdict schema
    at_gate3 = _any_project_at_gate3()
    system_prompt = _get_definition_context(def_role, include_verdict_schema=at_gate3) if def_role else OBSERVER_SYSTEM_PROMPT

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    max_turns = 5
    # 定义层模式不需要工具调用，简化对话
    use_tools = not def_role
    with httpx.Client(timeout=60.0) as client:
        for _ in range(max_turns):
            body = {
                "model": model,
                "messages": messages,
                "temperature": cfg.get("temperature", 0.3),
                "max_tokens": cfg.get("max_tokens", 2048 if def_role else 1024),
            }
            if use_tools:
                body["tools"] = OBSERVER_TOOLS
                body["tool_choice"] = "auto"

            try:
                resp = client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                return f"调用 LLM 失败：{e}"

            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})

            tool_calls = message.get("tool_calls") or []
            content = message.get("content")

            # 定义层模式：直接返回文本
            if def_role:
                return content.strip() if content else "（模型返回空内容）"

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
