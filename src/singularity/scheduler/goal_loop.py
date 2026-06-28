"""Goal 循环 — 目标导向的多轮迭代执行 (借鉴 Scream Code)。

一个 Goal 任务会反复执行直到:
  1. 产出满足 Goal 条件 → 成功
  2. 达到最大迭代次数 → 失败
  3. 连续两轮无改善 → 早停

用法:
  loop = GoalLoop(agents)
  result = loop.run(task, goal="修复所有 lint 错误", max_iter=5)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler._types import RunContext
from singularity.scheduler._exec import _run_with_retry
from singularity.scheduler import snapshot as snap_mod
from singularity.scheduler import tracker
from singularity.scheduler.tracker import TaskStatus


@dataclass
class GoalResult:
    success: bool
    iterations: int = 0
    final_output: str = ""
    history: list[dict] = field(default_factory=list)  # [{iter, output, goal_met, reason}]
    error: str = ""


class GoalLoop:
    """目标循环执行器。"""

    def __init__(self, agents: dict):
        self._agents = agents

    def run(self, task, goal: str, max_iter: int = 5) -> GoalResult:
        """反复执行 task，直到产出满足 goal 或耗尽迭代。

        每轮:
          1. 注入 goal 上下文到 task description
          2. 执行 task
          3. LLM 判定是否满足 goal
          4. 满足 → 返回；不满足 → 下轮（带反馈）
        """
        history: list[dict] = []
        last_output = ""

        for i in range(1, max_iter + 1):
            # 构造本轮输入: 原始描述 + goal + 上轮反馈
            prompt = task.description
            if i == 1:
                prompt = f"[Goal] {goal}\n\n任务: {prompt}"
            else:
                feedback = self._extract_feedback(last_output, goal)
                prompt = f"[Goal] {goal} (第{i}轮改进)\n上一轮产出问题: {feedback}\n\n任务: {task.description}"

            # 执行
            ctx = RunContext(batch_id=f"goal_{task.id}_{i}", snapshot_ref="", merge_queue=None)
            batch = _run_with_retry(task, ctx, self._agents)

            if not batch.ok or not batch.dispatch_result:
                history.append({"iter": i, "output": "", "goal_met": False, "reason": batch.term_reason})
                continue

            output = batch.dispatch_result.executor_result.raw_output
            last_output = output

            # LLM 判定 goal 是否满足
            goal_check = self._check_goal(output, goal, task.description)
            met = goal_check.get("met", False)
            reason = goal_check.get("reason", "")

            history.append({"iter": i, "output": output[:200], "goal_met": met, "reason": reason})

            if met:
                return GoalResult(success=True, iterations=i, final_output=output, history=history)

            # 早停: 连续两轮无改善
            if i >= 3 and not any(h["goal_met"] for h in history[-2:]):
                return GoalResult(success=False, iterations=i, final_output=output,
                                  history=history, error="连续两轮无改善，早停")

        return GoalResult(success=False, iterations=max_iter, final_output=last_output,
                          history=history, error=f"达到最大迭代 {max_iter}")

    def _check_goal(self, output: str, goal: str, task_desc: str) -> dict:
        """LLM 判定产出是否满足 goal。返回 {"met": bool, "reason": str}。"""
        prompt = f"""判断以下任务产出是否满足了目标。

目标: {goal}
任务: {task_desc[:200]}
产出 (前500字): {output[:500]}

只回答 JSON: {{"met": true/false, "reason": "一句话原因"}}
如果产出基本满足了目标要求 → met=true。不确定 → met=false。"""

        try:
            e_agents = self._agents.get("any", [])
            if not e_agents:
                return {"met": False, "reason": "no_judge_agent: 无 E 层 agent 可判定 goal"}
            e_cfg = e_agents[0]
            import os
            api_key = os.environ.get(e_cfg.get("api_key_env", ""), "")
            if not api_key:
                return {"met": False, "reason": "no_api_key: 未配置 E 层 API key"}
            base_url = e_cfg.get("entry", e_cfg.get("base_url", "https://api.deepseek.com/v1"))
            model = e_cfg.get("request_template", {}).get("model", e_cfg.get("model", "deepseek-chat"))

            body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 100, "temperature": 0.1}
            client = httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0))
            resp = client.post(f"{base_url}/chat/completions", json=body,
                               headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            # 提取 JSON
            import re
            m = re.search(r'\{[^}]+\}', content)
            if m:
                return json.loads(m.group())
            return {"met": "true" in content.lower(), "reason": content[:100]}
        except Exception as e:
            return {"met": False, "reason": f"judge_unavailable: {e}"}

    def _extract_feedback(self, output: str, goal: str) -> str:
        """从上一轮产出提取改进反馈 (规则版本，不调LLM)。"""
        issues = []
        if len(output) < 80:
            issues.append("产出过短")
        if "error" in output.lower() or "错误" in output:
            issues.append("产出包含错误信息")
        if not issues:
            return f"上一轮产出未完全满足目标 '{goal[:60]})'，请改进"
        return "问题: " + "; ".join(issues)
