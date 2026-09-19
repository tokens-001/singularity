"""认不出来的 400，**必须把现场留下**。

来历（2026-09-19 夜，一整轮真机死在这上面）：任务报
`400 The reasoning_content in the thinking mode must be passed back`，
等回头看时**现场什么都没有** ——
  · trace 只留了"超时那次"（`_save_trace` 幂等，先写者胜）；
  · `alerts.jsonl` 只有告警键，没有请求体。
只能事后拿探针去**重撞**，四个形状全是 200，**触发条件至今没找到**。

两条判据，缺一不可：
  ① 认不出来的 400 → 落一条（含**原样**的 `messages`，不是摘要）；
  ② 认得出、代码自己会处理的那两类（`tool_choice` / 思考参数）→ **不落**
     （每轮都记会把真信号淹掉，同 `_SLOW_CALL_LOG_S` 只记 ≥20s 的理由）。
"""
import json

import pytest

from singularity.scheduler import config
from singularity.scheduler.executors import openai_agent as oa

_DUMP = "llm_400_unknown.jsonl"


def _dumps() -> list[dict]:
    p = config.QIDIAN_DIR / _DUMP
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _executor(monkeypatch) -> oa.OpenAIAgentExecutor:
    monkeypatch.setenv("TEST_KEY", "k")
    cfg = {"model": "m", "api_key_env": "TEST_KEY", "entry": "http://x"}
    return oa.OpenAIAgentExecutor(cfg, "任务", "tid-400", skill_tools=[], mcp_tools=[])


def test_认不出来的_400_留下完整现场(monkeypatch):
    """这正是 09-19 夜那条 400 会走到的分支（`reasoning_content` 不属于任何已知类别）。"""
    ex = _executor(monkeypatch)
    raw = ('HTTP 400: {"error":{"message":"The `reasoning_content` in the thinking '
           'mode must be passed back to the API.","type":"invalid_request_error"}}')
    monkeypatch.setattr(ex, "_api_call", lambda body: (_ for _ in ()).throw(oa._FormatError(raw)))

    res = ex.run()
    assert res.success is False, "认不出来的 400 本来就该判失败"

    got = _dumps()
    assert len(got) == 1, f"该落一条现场，落了 {len(got)} 条"
    rec = got[0]
    assert "reasoning_content" in rec["error"], "错误原文没留下"
    assert rec["task_id"] == "tid-400"
    # ⚠️ **原样落，不许摘成摘要** —— 触发条件可能就藏在"某条消息有没有某个字段"上
    assert isinstance(rec["messages"], list) and rec["messages"], "请求体没留下"
    assert rec["messages"][0]["role"] == "system", "留下的不是请求体"
    assert rec["messages"][1]["content"] == "任务", "消息被改写过"


def test_认得出的_400_不落(monkeypatch):
    """`tool_choice` 那类代码自己会降级处理 —— 每轮都记会把真信号淹掉。"""
    ex = _executor(monkeypatch)
    seen: list[str] = []

    def fake(body):
        seen.append(body.get("tool_choice"))
        if body.get("tool_choice") == "required":
            raise oa._FormatError(
                'HTTP 400: {"error":{"message":"Thinking mode does not support '
                'this tool_choice"}}')
        return {"choices": [{"message": {"content": "收尾"}}]}

    monkeypatch.setattr(ex, "_api_call", fake)
    monkeypatch.setattr(ex, "_execute_tool", lambda name, args: "ok")
    ex.run()

    assert "required" in seen and "auto" in seen, f"没走到降级分支，测的是空气：{seen}"
    assert _dumps() == [], "认得出来的 400 也被记了 —— 这台账会变成噪声"


def test_落盘失败要出声不许静默(monkeypatch):
    """这条通道是"现场"的唯一来源：它哑了，和"没发生过 400"长得一模一样。"""
    from singularity.scheduler import witness
    ex = _executor(monkeypatch)

    class _Boom:
        def __truediv__(self, other):
            raise OSError("磁盘满了")

    monkeypatch.setattr(config, "QIDIAN_DIR", _Boom())
    heard: list[str] = []
    monkeypatch.setattr(witness, "warn", lambda scope, msg, **kw: heard.append(msg))

    ex._dump_unknown_400({"messages": [], "model": "m"}, "HTTP 400: 怪东西")

    assert any("unknown_400_dump_failed" in m for m in heard), \
        f"落盘失败一声不吭：{heard}"
