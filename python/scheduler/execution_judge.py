"""execution_judge.py — 执行裁判模块

每个 Agent 执行完成后，裁判用便宜的 E 层模型快速判断：
- 任务是否真的完成了？（对比描述和实际产出）
- 质量是否可接受？
不合格的自动打回重试，最多 3 次，每次换不同的模型。

依赖：scheduler 通用 LLM 调用、tracker 状态读写。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from ._io import try_parse_json

_log = logging.getLogger(__name__)


@dataclass
class JudgeVerdict:
    """裁判判定结果。"""
    pass_: bool                    # 是否通过
    score: float = 0.0            # 0-1 质量分
    reason: str = ""              # 失败原因（注入下一轮 Reflexion）
    failure_mode: str = ""        # 失败模式分类：tool_loop / empty_output / json_error / semantic_error / context_insufficient / unknown
    uncertain: bool = False       # 不确定时给 pass=true 但标记 uncertain，宁可放过不冤杀

    def to_dict(self) -> dict:
        return {
            "pass": self.pass_, "score": self.score, "reason": self.reason,
            "failure_mode": self.failure_mode, "uncertain": self.uncertain,
        }


def judge(task_description: str, agent_output: str, task_type: str = "default") -> JudgeVerdict:
    """裁判入口：对比任务描述和 Agent 产出，返回结构化判定。

    先做低成本预检（空输出、不合法 JSON 等），预检不通过直接返回 fail，
    省掉一次 LLM 调用。
    """
    # ── 预检层：低成本拦截明显失败 ──
    pre = _pre_check(agent_output)
    if pre:
        return pre

    # ── LLM 裁判层 ──
    return _llm_judge(task_description, agent_output, task_type)


def _pre_check(output: str) -> Optional[JudgeVerdict]:
    """低成本预检：空输出、非法 JSON、tool-loop 残留检测。"""
    if not output or not output.strip():
        return JudgeVerdict(
            pass_=False, score=0.0, reason="空输出",
            failure_mode="empty_output",
        )

    # 检测 tool-loop 残留（Agent 输出中包含未完成的工具调用标记）
    tool_loop_markers = [
        '{"tool_calls"', '"function_call"', '```json\n{"name":',
        'I need to read', 'Let me search', 'Let me check the file',
    ]
    loop_count = sum(1 for m in tool_loop_markers if m.lower() in output.lower())
    if loop_count >= 3:
        return JudgeVerdict(
            pass_=False, score=0.1, reason=f"检测到 tool-loop 残留({loop_count}处)",
            failure_mode="tool_loop",
        )

    # 检测 JSON 格式错误（输出中包含未闭合的括号）
    if output.strip().startswith("{") or output.strip().startswith("["):
        try:
            json.loads(output.strip())
        except json.JSONDecodeError:
            return JudgeVerdict(
                pass_=False, score=0.2, reason="输出 JSON 格式不合法",
                failure_mode="json_error",
            )

    return None


def _llm_judge(task_desc: str, output: str, task_type: str) -> JudgeVerdict:
    """调用 E 层模型做语义判断。"""
    prompt = _build_judge_prompt(task_desc, output, task_type)
    raw = _call_e_layer(prompt)

    return _parse_verdict(raw)


def _build_judge_prompt(task_desc: str, output: str, task_type: str) -> str:
    """构建裁判 prompt——防偏见：不说'这是 Agent 的输出请打分'，
    改为'请判断这段代码修改是否完成了以下需求'。"""
    output_snippet = output[:3000]  # 截断避免 token 浪费
    return f"""请判断以下代码/文本修改是否完成了所述需求。

【需求描述】
{task_desc}

【任务类型】{task_type}

【实际产出】
{output_snippet}

【判断标准】
- 是否完成了需求描述中的所有要点？
- 产出是否可以直接使用（不需要人工修补）？
- 如果产出看起来正确但你不完全确定，标记 uncertain=true

【输出格式】只输出 JSON：
{{"pass": true/false, "score": 0.0-1.0, "reason": "一句话原因", "failure_mode": "semantic_error/context_insufficient/ok", "uncertain": true/false}}"""


def _parse_verdict(raw: str) -> JudgeVerdict:
    """解析 LLM 返回的 JSON verdict。"""
    if not raw:
        return JudgeVerdict(pass_=True, score=0.5, reason="裁判未返回结果，默认放行",
                            failure_mode="unknown", uncertain=True)

    # 用 _io.try_parse_json 统一提取 JSON
    result = try_parse_json(raw)
    if not result.get("parse_error"):
        return JudgeVerdict(
            pass_=bool(result.get("pass", True)),
            score=float(result.get("score", 0.5)),
            reason=str(result.get("reason", "")),
            failure_mode=str(result.get("failure_mode", "unknown")),
            uncertain=bool(result.get("uncertain", False)),
        )

    return JudgeVerdict(pass_=True, score=0.5, reason="裁判结果解析失败，默认放行",
                        failure_mode="unknown", uncertain=True)


def _call_e_layer(prompt: str) -> str:
    """调 E 层最便宜模型。"""
    import os
    try:
        import httpx
        # 从环境获取第一个可用的 E 层 API
        for env_var, base_url, model in [
            ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/chat/completions", "deepseek-chat"),
            ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-5-turbo"),
            ("KIMI_API_KEY", "https://api.moonshot.cn/v1/chat/completions", "kimi-k2.7-code"),
        ]:
            api_key = os.environ.get(env_var, "")
            if api_key:
                with httpx.Client(timeout=httpx.Timeout(30)) as client:
                    r = client.post(
                        base_url,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": "你是代码审查裁判。只输出要求的 JSON。"},
                                {"role": "user", "content": prompt},
                            ],
                            "max_tokens": 256,
                            "temperature": 0.1,
                        },
                    )
                    if r.status_code == 200:
                        data = r.json()
                        return data["choices"][0]["message"]["content"]
                    else:
                        _log.warning(f"Judge LLM {env_var} returned {r.status_code}")
        return ""
    except Exception as e:
        _log.warning(f"Judge LLM call failed: {e}")
        return ""


# ═══════════════════════════════════════════════
# 重试策略
# ═══════════════════════════════════════════════

def should_retry(verdict: JudgeVerdict, retry_count: int, max_retries: int = 3) -> bool:
    """判断是否应该重试。信息不足不重试。"""
    if verdict.pass_:
        return False
    if retry_count >= max_retries:
        return False
    if verdict.failure_mode == "context_insufficient":
        return False  # 缺少上下文，重试无意义
    return True


def build_reflexion_feedback(verdict: JudgeVerdict) -> str:
    """构建下一轮的 Reflexion 提示。"""
    return (
        f"[上一轮结果不合格]\n"
        f"原因: {verdict.reason}\n"
        f"失败模式: {verdict.failure_mode}\n"
        f"请修正后重新输出。"
    )
