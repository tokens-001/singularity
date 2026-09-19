"""流停滞要有自己的 `error_kind`，但**走路和别人一模一样**（2026-09-19 复核审计 A6）。

代码里那条 `raise` 的理由是**对的**（注释原话：返回空会被上层当成"这次调用成功了、
只是模型没说话"，同一个卡住的模型继续被派下一轮；**抛错才走 failover**），
归因也对（停滞是服务端/网络的事，不是我们的刀）。审计把它列进"归因错"是**列错了**。

剩下的那一半和 A3 同形：它和"模型吐了个空"在 `error_kind` 上原来**长得一样**
（都是 `exec`），只差 error 文本里那几个字。

🔴 **判定一个字没动**：两条断言钉住 —— ① 仍是 `_NetworkError` **子类**（否则
`except (_NetworkError, _FormatError)` 接不住，异常会穿出去 = 行为变了）；
② `_dispatch_exec` 对它**照旧记 breaker**（别偷偷开恩）。
"""
import inspect

from singularity.scheduler.executors import openai_agent as oa


def test_停滞异常仍是网络错误的子类():
    """**这条最要紧**：捕获点写的是 `except (_NetworkError, _FormatError)` ——
    改成平级的新异常，那几处就接不住，异常会直接穿出去。"""
    assert issubclass(oa._StalledError, oa._NetworkError)


def test_两处流停滞都抛这个子类():
    src = inspect.getsource(oa)
    assert src.count("raise _StalledError(") == 2, (
        "流停滞的抛出点不止/不足两处（`_idle > _STALL_TIMEOUT` 和 httpx ReadTimeout）")
    assert "raise _NetworkError(f\"流停滞" not in src, "还有一处没换过来"


def test_停滞照旧记breaker_别偷偷开恩(monkeypatch):
    """**对照**：`timeout` 免熔断是因为那是**我方给的时限**；停滞不是 ——
    连续 3 次停滞说明这家现在不好用，熔断 300 秒正是为此设计的。"""
    import pytest

    from singularity.scheduler import _dispatch_exec as pd
    from singularity.scheduler.executors.base import ExecutorResult

    calls = []
    monkeypatch.setattr(pd, "pick_agent_fallback_chain",
                        lambda *a, **k: [{"model": "m", "type": "openai-agent"}])
    monkeypatch.setattr(pd, "_prefer_by_strengths", lambda task, chain: chain)
    monkeypatch.setattr(pd, "_committee_allowed", lambda *a, **k: False)
    monkeypatch.setattr(pd, "_ensure_agent_type", lambda c: c)
    monkeypatch.setattr(pd, "_model_breaker",
                        type("B", (), {"record_failure": lambda _s, m: calls.append(m),
                                       "record_success": lambda *a: None})())
    monkeypatch.setattr(pd, "_run_executor",
                        lambda *a, **k: ExecutorResult(success=False, raw_output="",
                                                       error="流停滞 90s 无新 token",
                                                       error_kind="stalled"))

    with pytest.raises(RuntimeError):
        pd.dispatch("任务", "any", "tid", {})

    assert calls == ["m"], f"stalled 被开了恩（没记 breaker）：{calls}"
