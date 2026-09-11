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
    cached_at: float = 0.0


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

# 分类器**只该**吐这五个值（上面 prompt 里写死的）。**必须校验**：
# 以前是 `parsed.get("type", "default")` —— 模型吐个别的写法（"bugFix"、"修复"）
# 会被**原样存下**，下游 `validator._annotate_unverified` 按字面比 `== "bugfix"` 就全不命中，
# 那三条"未验证"标注**静默消失且不报错**。这就是本仓最典型的那种坏法
# （防御模式 §44：承诺类字段的"没发生"必须能被查出来，不能是悄悄少做一件事）。
_VALID_TASK_TYPES = frozenset({"bugfix", "feature", "refactor", "docs", "default"})


def _parse_classify_reply(content: str) -> RouteResult:
    """把分类器的回复解析成 RouteResult。

    抽成纯函数是为了**能直接测**：这段以前一行测试都没有，而它的坏法是静默的
    （错值原样存下 → 下游按字面比不中 → 少做几件事，不报错）。
    """
    import re
    m = re.search(r'\{[^}]+\}', content or "")
    if not m:
        return RouteResult(task_type="default")
    parsed = json.loads(m.group())
    if not isinstance(parsed, dict):
        return RouteResult(task_type="default")

    raw_type = parsed.get("type", "default")
    # 先判 str 再判成员 —— `in frozenset` 遇到 list/dict 会抛 TypeError
    # （模型偶尔会吐 `"type": ["bugfix"]` 这种）。这里必须自己挡住：
    # 靠外层 `except Exception` 兜底的话，整条分类结果都会被丢成 RouteResult()，
    # 连模型明确给的 gate/signals 一起没了。
    if not isinstance(raw_type, str) or raw_type not in _VALID_TASK_TYPES:
        # 落一条告警再回退 —— 回退本身是对的，但"悄悄回退"会让
        # "分类器老吐怪值"这件事永远没人知道。可查 > 干净。
        try:
            from singularity.scheduler import witness
            witness.warn("router", f"invalid_task_type:{raw_type!r} → default"[:200])
        except Exception:
            pass
        raw_type = "default"

    return RouteResult(
        task_type=raw_type,
        gate_required=parsed.get("gate", False),
        matched_signals=parsed.get("signals", []),
    )


def _llm_classify(task: str) -> RouteResult:
    """用 LLM 分类任务类型。失败时回退 default。"""
    # 先查缓存 (未过期则直接返回)
    if task in _CLASSIFY_CACHE:
        cached = _CLASSIFY_CACHE[task]
        if cached.cached_at and time.time() - cached.cached_at < _CACHE_EXPIRY:
            return cached

    try:
        # 从 agents.json 取任意可用模型
        from singularity.scheduler import dispatcher as disp_mod
        agents = disp_mod.load_agents()
        agent_cfg = None
        for tier in ("any",):
            for a in agents.get(tier, []):
                if not a.get("model"):
                    continue
                # agent_api_available 会**就地**补全 type/provider/entry/api_key_env ——
                # 直接读 agents_custom.json 拿到的 entry/api_key_env 都是空串
                # （真值在模型注册表里，由 _build_agent_from_registry 补）。
                # 不补的话下面 api_key 为空 → 永远 return RouteResult() →
                # **所有任务都被判成 default**，路由分类整体失效。
                if disp_mod.agent_api_available(a) and a.get("entry"):
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

        # entry 可能本来就是完整端点（注册表里存的是 .../v1/chat/completions），
        # 无脑再拼一次会得到 .../chat/completions/chat/completions → 404。
        url = base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"

        client = httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
        resp = client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        # 每建一个任务都要过这里做分类。以前不记账 = 这条最频繁的调用完全不在统计里。
        try:
            from singularity.scheduler._token_budget import record_system_tokens
            _tk = int((data.get("usage") or {}).get("total_tokens", 0) or 0)
            if _tk > 0:
                record_system_tokens(model=model, level="router", tokens=_tk)
        except Exception:
            pass          # 记账失败不能影响分类
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        result = _parse_classify_reply(content)
    except Exception:
        result = RouteResult()

    # 写缓存 (限制大小)
    if len(_CLASSIFY_CACHE) >= _CACHE_MAX:
        _CLASSIFY_CACHE.pop(next(iter(_CLASSIFY_CACHE)))
    result.cached_at = time.time()
    _CLASSIFY_CACHE[task] = result
    return result


@timed(name="router")
def route(task: str) -> RouteResult:
    """LLM 分类任务。失败时返回 default。"""
    # ponytail: 短任务描述 (<20字) 直接用 default, 不值得调 LLM
    if len(task) < 20:
        return RouteResult()
    return _llm_classify(task)


