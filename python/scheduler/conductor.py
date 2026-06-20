"""conductor.py — 项目流程自动推进 Agent (Conductor)

一个轻量的 E 层 Agent，只干一件事：
盯着项目当前阶段，自动决定下一步，推进流程直到完成或需要人工介入。

用法：
  conductor.auto_advance(project_id)  # 一次性推到下一个卡点
  conductor.start_autopilot(project_id)  # 启动后台自动推进线程
"""
from __future__ import annotations


import json, re as _re, time, threading, logging
from pathlib import Path
from typing import Any

from . import project as _proj
from .project import Phase
from . import dispatcher as _dispatch
from . import workflow as _wf

_log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
# Gate 自动判分 Prompt
# ═══════════════════════════════════════════════
GATE_JUDGE_PROMPT = """你是项目 Gate 自动裁判。判断当前阶段产出是否满足进入下一阶段的标准。

【项目需求】
{description}

【当前阶段】
{phase}

【产出摘要】
{artifacts_summary}

【判断标准】
- 调研阶段产出：至少有 3 条有效引用，有明确的推荐方案
- 架构阶段产出：有清晰的任务分解（≥2 个任务），有约束清单
- 执行阶段产出：所有任务已完成，无未处理 bug
- 审查阶段产出：发现的问题已修复或标记为已知
- 修复阶段产出：所有 bug 已处理

【输出格式】
只输出 JSON，不要任何其他内容：
{{"pass": true/false, "reason": "一句话原因", "next_action": "approved/rejected/retry"}}
"""


def _call_llm(prompt: str, level: str = "E") -> str:
    """用 E 层最便宜模型调一次 LLM，返回原始文本。不调工具，纯推理。"""
    import os, httpx
    from . import dispatcher as _disp

    agents = {}
    try:
        agents = _disp.load_agents()
    except Exception:
        return ""

    tier_agents = agents.get(level, [])
    if not tier_agents:
        return ""

    # 用第一个可用的 E 层 agent
    cfg = tier_agents[0]
    api_key = os.environ.get(cfg.get("api_key_env", ""), "")
    base_url = cfg.get("entry", "")
    model = cfg.get("model", "")
    tmpl = cfg.get("request_template", {})

    if not api_key or not base_url:
        return ""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": tmpl.get("model", model),
        "messages": [
            {"role": "system", "content": "只输出要求的 JSON，不要额外内容。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 512,
        "temperature": 0.1,
    }

    try:
        with httpx.Client(timeout=httpx.Timeout(30)) as client:
            r = client.post(f"{base_url}/chat/completions", headers=headers, json=body)
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        _log.warning(f"Conductor LLM call failed: {e}")

    return ""


def _summarize_artifacts(p: Any) -> str:
    """提取项目当前产出的摘要文本。"""
    parts = []
    rr = p.research_report
    if rr:
        refs = rr.get("references", []) or []
        rec = rr.get("recommendation", "") or ""
        parts.append(f"调研报告: {len(refs)}条引用, 推荐: {rec[:200]}")
    arch = p.architecture
    if arch:
        tasks = arch.get("tasks", []) or []
        cons = arch.get("constraints", []) or []
        parts.append(f"架构方案: {len(tasks)}个任务, {len(cons)}条约束")
    issues = p.issues or []
    if issues:
        bugs = sum(1 for i in issues if i.get("severity") == "bug")
        parts.append(f"问题清单: {len(issues)}个 (bug={bugs})")
    return "\n".join(parts) if parts else "暂无产出"


# ═══════════════════════════════════════════════
# 核心推进逻辑
# ═══════════════════════════════════════════════
def auto_advance(project_id: str, agents: dict | None = None) -> dict:
    """自动推进一个项目到下一个阶段。

    返回: {"ok": bool, "phase": str, "action": str, "message": str}
    """
    p = _proj.load(project_id)
    if not p:
        return {"ok": False, "phase": "?", "action": "error", "message": "项目不存在"}

    phase = p.phase
    if agents is None:
        try:
            agents = _dispatch.load_agents()
        except Exception:
            agents = {}

    # ═══ Template → 自动启动 ═══
    if phase == Phase.TEMPLATE:
        if not p.description:
            return {"ok": False, "phase": phase.value, "action": "blocked",
                    "message": "缺少需求描述，无法自动启动"}
        try:
            _wf.start_project_workflow(p, agents)
            _proj.save(p)
            return {"ok": True, "phase": p.phase.value, "action": "started",
                    "message": f"自动启动 → {p.phase.value}"}
        except Exception as e:
            return {"ok": False, "phase": phase.value, "action": "error",
                    "message": f"启动失败: {e}"}

    # ═══ Gate 阶段 → 自动判分 ═══
    if phase.value.startswith("gate"):
        artifacts = _summarize_artifacts(p)
        if not artifacts or artifacts == "暂无产出":
            # 没有产出就自动打回
            p.confirm_gate(phase, "rejected")
            _proj.save(p)
            return {"ok": True, "phase": p.phase.value, "action": "auto_rejected",
                    "message": f"无产出，自动打回 {phase.value}"}

        # 调用 E 层模型判分
        judge_prompt = GATE_JUDGE_PROMPT.format(
            description=(p.description or "")[:500],
            phase=phase.value,
            artifacts_summary=artifacts,
        )
        raw = _call_llm(judge_prompt, level="E")

        # 解析裁判结果
        passed = _parse_judge_result(raw)
        decision = "approved" if passed else "rejected"
        p.confirm_gate(phase, decision)
        _proj.save(p)

        return {
            "ok": True, "phase": p.phase.value, "action": f"auto_{decision}",
            "message": f"{phase.value} 自动{'批准' if passed else '打回'}: {raw[:100]}"
        }

    # ═══ 执行阶段 → 自动触发 ═══
    if phase in (Phase.RESEARCHING, Phase.PLANNING, Phase.EXECUTING,
                 Phase.REVIEWING, Phase.FIXING):
        if not agents:
            return {"ok": False, "phase": phase.value, "action": "blocked",
                    "message": "无可用 Agent，无法执行"}
        try:
            _wf.run_phase(p, agents)
            _proj.save(p)
            return {"ok": True, "phase": p.phase.value, "action": "executed",
                    "message": f"执行 {phase.value} → 请等待后台完成"}
        except Exception as e:
            return {"ok": False, "phase": phase.value, "action": "error",
                    "message": f"执行失败: {e}"}

    # ═══ Done → 无需推进 ═══
    if phase == Phase.DONE:
        return {"ok": True, "phase": phase.value, "action": "done",
                "message": "项目已完成"}

    return {"ok": False, "phase": phase.value, "action": "unknown",
            "message": f"未知阶段: {phase.value}"}


def _parse_judge_result(raw: str) -> bool:
    """解析裁判 LLM 的输出，提取 pass 字段。"""
    if not raw:
        return True  # 无法判断时默认放行（避免卡住）

    # 尝试多种 JSON 提取方式
    candidates = []

    # 1. ```json ... ``` 块
    for m in _re.finditer(r"```(?:json)?\s*\n?(.*?)```", raw, _re.DOTALL):
        candidates.append(m.group(1).strip())

    # 2. 裸 {...} 块
    for m in _re.finditer(r"\{[^{}]*\}", raw):
        candidates.append(m.group(0).strip())

    # 3. 整体当做 JSON
    candidates.append(raw.strip())

    for c in candidates:
        try:
            obj = json.loads(c)
            return bool(obj.get("pass", True))
        except (json.JSONDecodeError, TypeError):
            # JSON 修复：去掉尾部逗号重试
            try:
                fixed = _re.sub(r",\s*}", "}", c)
                fixed = _re.sub(r",\s*]", "]", fixed)
                obj = json.loads(fixed)
                return bool(obj.get("pass", True))
            except (json.JSONDecodeError, TypeError):
                continue

    return True  # 全部解析失败，默认放行


# ═══════════════════════════════════════════════
# 自动推进循环（后台线程）
# ═══════════════════════════════════════════════
_autopilot_threads: dict[str, threading.Thread] = {}
_autopilot_stop: dict[str, bool] = {}


def start_autopilot(project_id: str) -> dict:
    """启动后台自动推进线程。

    线程会持续推进项目直到 done 或遇到需要人工介入的问题。
    """
    if project_id in _autopilot_threads:
        t = _autopilot_threads[project_id]
        if t.is_alive():
            return {"ok": False, "message": "自动推进已在运行中"}
        del _autopilot_threads[project_id]

    _autopilot_stop[project_id] = False

    def _run():
        agents = {}
        try:
            agents = _dispatch.load_agents()
        except Exception:
            pass

        max_steps = 30  # 最多 30 步（防止死循环）

        for step in range(max_steps):
            if _autopilot_stop.get(project_id, False):
                _log.info(f"Autopilot stopped for {project_id}")
                break

            p = _proj.load(project_id)
            if not p:
                break
            if p.phase == Phase.DONE:
                _log.info(f"Autopilot: project {project_id} done")
                break

            prev_phase = p.phase
            result = auto_advance(project_id, agents)

            _log.info(f"Autopilot[{step}]: {prev_phase.value} → {result}")

            # 如果是后台执行阶段，等它完成
            p2 = _proj.load(project_id)
            if p2 and p2.phase == prev_phase and p2.phase not in (Phase.DONE, Phase.TEMPLATE):
                if not p2.phase.value.startswith("gate"):
                    # 等待阶段完成（轮询）
                    waited = 0
                    while waited < 300:  # 最多等 5 分钟
                        if _autopilot_stop.get(project_id, False):
                            break
                        time.sleep(10)
                        waited += 10
                        p3 = _proj.load(project_id)
                        if p3 and p3.phase != prev_phase:
                            break

            # Gate 打回时暂停一下（给修复留时间）
            if result.get("action") == "auto_rejected":
                time.sleep(5)

        del _autopilot_threads[project_id]
        _autopilot_stop.pop(project_id, None)
        _log.info(f"Autopilot finished for {project_id}")

    t = threading.Thread(target=_run, daemon=True)
    _autopilot_threads[project_id] = t
    t.start()

    return {"ok": True, "message": "自动推进已启动"}


def stop_autopilot(project_id: str) -> dict:
    """停止后台自动推进。"""
    _autopilot_stop[project_id] = True
    return {"ok": True, "message": "已发送停止信号"}
