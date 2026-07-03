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

    """从 _TOOL_REGISTRY 自动分发, 加工具无需改此处。"""
    try:
        for t in _TOOL_REGISTRY:
            if t["name"] == name:
                # 只传工具声明的参数
                valid = {k: v for k, v in args.items() if k in t["params"]}
                result = t["handler"](**valid)
                return json.dumps(result, ensure_ascii=False, indent=2)
        return json.dumps({"error": f"unknown tool {name}"})
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
            "temperature": observer_cfg.get("temperature", 0.7),
            "max_tokens": observer_cfg.get("max_tokens", 4096),
        }
    # 默认走 DeepSeek
    return {
        "model": "deepseek-chat",
        "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "base_url": "https://api.deepseek.com/v1",
        "temperature": 0.7,
        "max_tokens": 4096,
    }


# ponytail: 状态上下文缓存, 省 ~30% token (状态无变化时复用)
_last_ctx: str = ""
_last_ctx_hash: str = ""

def _build_status_context(project_id: str = "") -> str:
    """预取全量系统状态，注入 prompt。只显示活跃任务，状态无变化时复用缓存。"""
    global _last_ctx, _last_ctx_hash
    status = _tool_get_system_status()
    tasks = _tool_list_tasks(limit=20, active_only=True, project_id=project_id)
    stalled = _tool_list_stalled_tasks()
    judge = _tool_get_judge_stats()
    recent = _tool_get_recent_events(limit=10)
    ctx = json.dumps({
        "系统状态": status,
        "最近任务": tasks,
        "卡住任务": stalled,
        "裁判统计": judge,
        "最近事件": recent,
    }, ensure_ascii=False, indent=2)
    # 简单 hash: 任务总数+运行数+最近更新时间的组合
    ctx_hash = f"{status.get('task_counts',{}).get('total',0)}:{status.get('running_total',0)}:{tasks[0].get('updated_at',0) if tasks else 0}"
    if ctx_hash == _last_ctx_hash and _last_ctx:
        return _last_ctx
    _last_ctx = ctx
    _last_ctx_hash = ctx_hash
    return ctx


DIRECT_SYSTEM_PROMPT = """你是 Singularity Dispatch 的主交互智能体。下面是当前系统的实时状态数据。根据这些数据回答用户问题，用户可以要求你创建任务或控制调度循环。

规则：
1. 基于提供的数据回答，不编造
2. 简洁准确，使用中文
3. 异常情况给出原因和建议
4. 数据中没有的信息，诚实说不知道
5. 用户要求创建任务时，引导他们使用完整功能模式"""


def _answer_question(question: str, project_id: str = "") -> str:
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

    # GATE1 确认检测: 定义阶段全部完成, 用户说通过 → 进入架构
    if project_id:
        session = _get_definition_session(project_id)
        if session.get("phase") == "gate1_waiting":
            q = question.strip().lower()
            if any(w in q for w in ("通过", "继续", "确认", "同意", "ok", "yes", "好", "可以", "行")):
                session["phase"] = "done"
                try:
                    from singularity.scheduler.project import load as load_project, Phase
                    proj = load_project(project_id)
                    if proj:
                        proj.confirm_gate(Phase.GATE1, "approved")
                except Exception:
                    pass
                return "✅ GATE1 已通过。进入架构阶段，系统架构师/AI架构师/前端架构师将并行设计方案。"
            elif any(w in q for w in ("修改", "改", "不对", "重来", "不通过")):
                session["phase"] = "defining"
                session["active_role"] = "product-manager"
                return "已退回定义阶段。请描述需要修改的内容，我从产品经理角色重新开始。"

    # GATE2/GATE3 确认检测
    if project_id:
        try:
            from singularity.scheduler.project import load as load_project, Phase, save as save_project
            proj = load_project(project_id)
            if proj:
                q = question.strip().lower()
                if proj.phase == Phase.GATE2:
                    if any(w in q for w in ("通过", "继续", "确认", "同意", "ok", "yes", "好", "可以", "行")):
                        proj.confirm_gate(Phase.GATE2, "approved")
                        return "✅ GATE2 已通过。进入实现阶段，前端/后端/数据/DevOps工程师将并行开发。"
                    elif any(w in q for w in ("修改", "改", "不对", "重来", "不通过")):
                        proj.confirm_gate(Phase.GATE2, "rejected")
                        return "已退回架构阶段。请描述需要修改的内容，将重新生成架构方案。"
                elif proj.phase == Phase.GATE3:
                    if any(w in q for w in ("通过", "继续", "确认", "同意", "ok", "yes", "好", "可以", "行")):
                        proj.confirm_gate(Phase.GATE3, "approved")
                        return "✅ GATE3 已通过。进入交付阶段，DevOps工程师将打包归档。"
                    elif any(w in q for w in ("修改", "改", "不对", "重来", "不通过")):
                        proj.confirm_gate(Phase.GATE3, "rejected")
                        return "已退回实现阶段。请描述需要修复的问题。"
        except Exception:
            pass

    # Step 3: 检测定义层意图，注入角色 prompt
    def_role = _detect_definition_intent(question)
    gate1_ready = False

    if def_role and project_id:
        # P0: 多轮定义会话 — 用会话状态驱动角色切换
        session = _get_definition_session(project_id)
        session["history"].append({"role": "user", "content": question})
        system_prompt, gate1_ready = _build_definition_prompt(project_id, question)
        use_tools = False
    elif def_role:
        # 无 project_id 的兼容路径 (旧行为)
        at_gate3 = _any_project_at_gate3()
        system_prompt = _get_definition_context(def_role, include_verdict_schema=at_gate3)
        use_tools = False
    else:
        system_prompt = OBSERVER_SYSTEM_PROMPT
        use_tools = True
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    # 根据任务复杂度自适应调整轮数
    if len(question) < 10 and not any(w in question for w in ("做", "写", "创建", "修", "改", "加", "build", "create", "fix")):
        max_turns = 1  # 纯闲聊/查询
    elif len(question) > 100 or any(w in question for w in ("架构", "系统", "设计", "多步", "完整", "全栈")):
        max_turns = 3  # 复杂任务, 可能需要多轮澄清
    else:
        max_turns = 2  # 常规任务
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

            # 定义层模式：后处理 — 检测产出完成 + 角色推进 + GATE1
            if def_role:
                text = content.strip() if content else "（模型返回空内容）"
                if not project_id:
                    return text
                session = _get_definition_session(project_id)
                role = session["active_role"]

                # 检测 [COMPLETE] 标记 → 保存产出 + 推进角色
                if "[COMPLETE]" in text:
                    # 提取 JSON 产出 (```json ... ``` 块)
                    import re as _re
                    json_match = _re.search(r'```json\s*(.*?)\s*```', text, _re.DOTALL)
                    artifact = json_match.group(1) if json_match else text
                    _save_definition_artifact(project_id, role, artifact)
                    session["history"].append({"role": role, "content": artifact})

                    next_role = _advance_definition_role(project_id, session)
                    if next_role:
                        text = text.replace("[COMPLETE]", "") + f"\n\n✅ {role} 产出已保存。接下来由 {next_role} 继续。"
                    else:
                        # 全部完成 → GATE1
                        text = text.replace("[COMPLETE]", "") + "\n\n---\n## 🛑 GATE1：定义阶段完成\n\n4份文档已产出（PRD/交互/UI方向/调研），请审核。审核通过后进入架构阶段。\n- 输入 **通过** 或 **继续** → 进入架构阶段\n- 输入修改意见 → 回到对应角色修正"
                return text

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
# P0: 定义阶段会话状态 (多轮对话, 角色切换, 文档产出)
# ═══════════════════════════════════════════════════════════════

_DEFINITION_SESSIONS: dict[str, dict] = {}
