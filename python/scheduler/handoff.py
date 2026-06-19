"""handoff.py — Agent 交接记录

每个 Agent 任务执行完后，在项目上留下结构化交接：
- 交付物位置
- 关键结论
- 下一个 Agent
- 是否需要人工确认

交接信息跟随任务产出沉淀，保证多 Agent 协作时不丢失上下文。
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Handoff:
    """一次 Agent 交接记录。"""
    task_id: str                  # 任务 ID
    agent_model: str              # 执行 Agent 的模型名
    phase: str                    # 阶段（researching / planning / executing / fixing 等）
    deliverable: str              # 交付物位置（文件路径或描述）
    conclusion: str               # 关键结论（裁判评语 + Agent 自己的总结）
    next_agent: str = ""          # 建议下一个 Agent 类型（Coding / QA / Reviewer / Human）
    human_confirm: bool = False   # 是否需要人工确认
    verdict: str = ""             # 裁判判分结果
    score: float = 0.0            # 质量评分
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = d.pop("timestamp")
        return d


def create_handoff(task, batch, project_id: str = "") -> Handoff | None:
    """从任务执行结果创建交接记录。

    优先解析 Agent 输出中的 [HANDOFF] 块，其次从 dispatcher 结果、
    裁判判分、项目阶段中推断。
    """
    disp = batch.dispatch_result
    verdict = getattr(batch, "judge_verdict", None)
    agent_model = "unknown"

    if disp and disp.agent_cfg:
        agent_model = disp.agent_cfg.get("model", "unknown")

    # 1. 尝试解析 Agent 输出的 [HANDOFF] 块
    raw_output = ""
    if disp and disp.executor_result:
        raw_output = disp.executor_result.raw_output or ""
    parsed = _parse_handoff_block(raw_output)

    # 2. 交付物
    deliverable = parsed.get("deliverable", "")
    if not deliverable and disp and disp.executor_result:
        files = disp.executor_result.changed_files or []
        if files:
            deliverable = "修改文件: " + ", ".join(files[:5])
        elif raw_output:
            deliverable = raw_output[:200]
    if not deliverable:
        deliverable = "无产出"

    # 3. 结论
    conclusion = parsed.get("conclusion", "")
    if not conclusion:
        if verdict:
            conclusion = verdict.reason
        elif batch.validation:
            conclusion = batch.validation.verdict
        else:
            conclusion = batch.term_reason

    # 4. 下一个 Agent
    next_agent = parsed.get("next", "")
    if not next_agent:
        if verdict and not verdict.pass_:
            next_agent = "Coding Agent（重试）"
        elif batch.term_reason and "pass" in batch.term_reason:
            next_agent = "QA Agent 或人工审查"

    # 5. 是否需要人工确认
    human_confirm = parsed.get("human_confirm", "").lower() == "true"
    if not human_confirm:
        if verdict and verdict.uncertain:
            human_confirm = True
        if batch.validation and batch.validation.action == "abort":
            human_confirm = True

    return Handoff(
        task_id=getattr(task, "id", ""),
        agent_model=agent_model,
        phase=getattr(task, "route_type", "") or "unknown",
        deliverable=deliverable,
        conclusion=conclusion,
        next_agent=next_agent,
        human_confirm=human_confirm,
        verdict="pass" if (verdict and verdict.pass_) else "fail",
        score=verdict.score if verdict else 0.0,
    )


def _parse_handoff_block(output: str) -> dict:
    """解析 Agent 输出末尾的 [HANDOFF] 块。

    格式:
    [HANDOFF]
    deliverable: <路径或描述>
    conclusion: <结论>
    next: <下一个 Agent>
    human_confirm: <true/false>
    """
    if not output or "[HANDOFF]" not in output:
        return {}
    idx = output.rfind("[HANDOFF]")
    block = output[idx:]
    result = {}
    for line in block.split("\n"):
        line = line.strip()
        if ":" in line and not line.startswith("["):
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def append_to_project(project_id: str, handoff: Handoff) -> None:
    """把交接记录追加到项目上。"""
    from . import project as _proj
    p = _proj.load(project_id)
    if not p:
        return
    if not hasattr(p, "handoffs") or p.handoffs is None:
        p.handoffs = []
    p.handoffs.append(handoff.to_dict())
    # 保留最近 50 条，避免无限膨胀
    if len(p.handoffs) > 50:
        p.handoffs = p.handoffs[-50:]
    _proj.save(p)
