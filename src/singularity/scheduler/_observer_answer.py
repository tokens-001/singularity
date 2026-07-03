"""观察者智能体 — 回答生成 + 定义层会话 + GATE 确认"""

from __future__ import annotations

import json
import logging
import time

import httpx

from singularity.scheduler._observer_definition import (
    _get_observer_cfg, _build_status_context, DIRECT_SYSTEM_PROMPT,
    _detect_definition_intent, _any_project_at_gate3, _get_definition_context,
    _execute_observer_tool, _definition_role_prompt,
)
from singularity.scheduler._observer_tools import OBSERVER_SYSTEM_PROMPT, OBSERVER_TOOLS

_log = logging.getLogger("observer")


# ═══════════════════════════════════════════════════════════════
# 只读查询工具（纯 Python 函数，直接读取现有数据）
# ═══════════════════════════════════════════════════════════════


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
    # 从 agent 配置读取 max_turns，默认工具模式 3、纯文本 1
    max_turns = int(cfg.get("max_turns", 3 if use_tools else 1))
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

