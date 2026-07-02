"""router.py — 任务类型识别 (LLM分类, 不再用正则)"""

from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field

import httpx

from singularity.scheduler import config
from singularity.scheduler.log import timed


@dataclass
class RouteResult:
    """路由结果: task_type + gate_required。"""
    task_type: str = "default"
    gate_required: bool = False
    matched_signals: list = field(default_factory=list)


# 任务分类缓存 (避免重复 LLM 调用)
_CLASSIFY_CACHE: dict[str, RouteResult] = {}
_CACHE_MAX = 200


_CACHE_EXPIRY = 3600  # 1 小时

_CLASSIFY_PROMPT = """你是任务分类器。判断用户描述属于哪种类型,只输出JSON。

类型:
- bugfix: 修bug、报错、异常、坏了、不对
- feature: 新增功能、模块、页面、组件
- refactor: 重构、重写、改架构、拆分
- docs: 文档、README、注释
- default: 其他

还要判断是否触及核心引擎文件(需要GATE门禁):
core.py, tokenizer.py, graph.py, search.py, config.py

只输出JSON,别说话:
{"type": "bugfix|feature|refactor|docs|default", "gate": true|false, "signals": ["命中关键词1"]}"""


def _llm_classify(task: str) -> RouteResult:
    """用 LLM 分类任务类型。失败时回退 default。"""
    # 先查缓存
    if task in _CLASSIFY_CACHE:
        cached = _CLASSIFY_CACHE[task]
        if time.time() - cached.matched_signals[0] if cached.matched_signals and isinstance(cached.matched_signals[0], (int, float)) else True:
            return cached

    try:
        # 从 agents.json 取任意可用模型
        from singularity.scheduler.dispatcher import load_agents
        agents = load_agents()
        agent_cfg = None
        for tier in ("any",):
            for a in agents.get(tier, []):
                if a.get("model"):
                    agent_cfg = a
                    break
            if agent_cfg:
                break

        if not agent_cfg:
            return RouteResult()

        model = agent_cfg.get("request_template", {}).get("model", agent_cfg.get("model", "deepseek-chat"))
        base_url = agent_cfg.get("entry", "https://api.deepseek.com/v1/chat/completions")
        api_key_env = agent_cfg.get("api_key_env", "DEEPSEEK_API_KEY")
        api_key = os.environ.get(api_key_env, "")

        if not api_key:
            return RouteResult()

        body = {
            "model": model,
            "messages": [{"role": "user", "content": f"任务描述: {task}\n\n{_CLASSIFY_PROMPT}"}],
            "max_tokens": 80, "temperature": 0,
        }

        client = httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
        resp = client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")

        # 提取 JSON
        import re
        m = re.search(r'\{[^}]+\}', content)
        if m:
            parsed = json.loads(m.group())
            result = RouteResult(
                task_type=parsed.get("type", "default"),
                gate_required=parsed.get("gate", False),
                matched_signals=parsed.get("signals", []),
            )
        else:
            result = RouteResult(task_type="default")
    except Exception:
        result = RouteResult()

    # 写缓存 (限制大小)
    if len(_CLASSIFY_CACHE) >= _CACHE_MAX:
        _CLASSIFY_CACHE.pop(next(iter(_CLASSIFY_CACHE)))
    _CLASSIFY_CACHE[task] = result
    return result


@timed(name="router")
def route(task: str) -> RouteResult:
    """LLM 分类任务。失败时返回 default。"""
    # ponytail: 短任务描述 (<20字) 直接用 default, 不值得调 LLM
    if len(task) < 20:
        return RouteResult()
    return _llm_classify(task)


def select_topology() -> dict:
    """基于 DAG 结构指标选择执行拓扑。"""
    try:
        from . import tracker
        m = tracker.dag_metrics()
    except Exception:
        return {"topology": "τS", "omega": 1, "delta": 1, "gamma": 0.0,
                "node_count": 1, "reason": "metrics_unavailable"}

    omega = m["omega"]; delta = m["delta"]; gamma = m["gamma"]; n = m["node_count"]

    if n <= 1:
        return {**m, "topology": "τS", "reason": "single_node"}
    if omega >= 3 and gamma < 0.3:
        return {**m, "topology": "τP", "reason": f"ω={omega}≥3 且 γ={gamma:.2f}<0.3 → 并行"}
    if delta >= 5 and gamma > 0.5:
        return {**m, "topology": "τH", "reason": f"δ={delta}≥5 且 γ={gamma:.2f}>0.5 → 层级"}
    if gamma > 0.5:
        return {**m, "topology": "τX", "reason": f"γ={gamma:.2f}>0.5 → 混合"}
    return {**m, "topology": "τS", "reason": f"ω={omega} δ={delta} γ={gamma:.2f} → 默认顺序"}
