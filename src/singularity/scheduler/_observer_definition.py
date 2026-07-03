"""观察者智能体 — 定义层：角色 prompt + 系统上下文 + 意图检测 + 工具分发"""
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
from singularity.scheduler._observer_tools import (
    _TOOL_REGISTRY, _tool_get_system_status, _tool_list_tasks,
    _tool_list_stalled_tasks, _tool_get_judge_stats, _tool_get_recent_events,
)

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
    # 用户明确要求跳过定义 → 直接执行
    skip_triggers = ["直接做", "直接开始", "别问了", "快做", "不用问", "不用说那么多",
                     "不要问", "少废话", "赶紧", "快开始", "马上做"]
    if any(t in q for t in skip_triggers):
        return ""
    # 产品/项目定义意图
    pm_triggers = ["我要做", "帮我设计", "做一个", "开发一个", "新产品", "新项目",
                   "立项", "prd", "产品方案", "需求分析"]
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


