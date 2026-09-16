"""`no_tools` 只是个**请求** —— 自带工具面的执行器忽略它，而原来只有一条告警就往下跑。

「禁工具」是三条路共同的前提（纯文本出方案，**别改磁盘**）：
  · 委员会成员（`_run_no_tools`）
  · A 臂自修订（也走 `_run_no_tools`）
  · 单模型"只出 JSON"的阶段：调研 / 架构（`dispatch(no_tools=True)`）
  · 委员会**合成**那一步（`_dispatch_committee`）

代价是实测过的：2026-09-11 **调研员在项目仓里把整个项目实现完了**（wc_lite.py ＋ 测试 ＋
真跑了一遍），合成 agent 把目标项目的架构写进了**奇点仓库自己的 `docs/`**。

⇒ 判定必须**挡在调用之前**。"出个声继续跑" = 假装拦住了（同
`_warn_if_profile_not_enforceable` 那条规矩）。

⚠️ **今天不会改变任何行为**（阵容里一个 `claude-cli` 都没有，三家都 `honors_no_tools=True`）
—— 它修的是"加一个 Claude agent 就醒"的那个雷。所以这里用假的执行器类型来验。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import singularity.scheduler._dispatch_exec as pd                    # noqa: E402
from singularity.scheduler.executors.base import ExecutorResult       # noqa: E402


class _CanHonor:
    honors_no_tools = True
    has_tool_surface = True


class _CannotHonor:                       # claude-cli 的形状
    honors_no_tools = False
    has_tool_surface = False


@pytest.fixture
def calls(monkeypatch):
    """记下**真的调了执行器几次** —— 光看返回值分不出"跳过了"和"跑了但没产出"。"""
    seen = []
    monkeypatch.setattr(pd, "_EXECUTOR_BY_TYPE",
                        {"ok": _CanHonor, "bad": _CannotHonor})
    monkeypatch.setattr(pd, "_run_executor",
                        lambda cls, *a, **k: (seen.append(cls.__name__),
                                              ExecutorResult(success=True, raw_output="答案"))[1])
    monkeypatch.setattr(pd, "_ensure_agent_type", lambda c: c)
    return seen


def test_run_no_tools_禁不掉就跳过不调(calls):
    """委员会成员 / A 臂那条（都走 `_run_no_tools`）：前提不成立 ⇒ **根本不调**。"""
    cfg = {"type": "bad", "model": "bad-1", "honors_no_tools": False}
    got = pd._run_no_tools(cfg, "提示", "tag", "any")
    assert got is None, "禁不掉工具的执行器不该跑（它接着会去改磁盘）"
    assert calls == [], f"居然真调了：{calls}"


def test_run_no_tools_能禁就照常跑(calls):
    """对照：能禁掉的照常跑 —— 别把修法改宽成"凡 no_tools 都跳过"。"""
    got = pd._run_no_tools({"type": "ok", "model": "ok-1"}, "提示", "tag", "any")
    assert got is not None and got[0] == "答案"
    assert calls == ["_CanHonor"]


def test_判据本身_认类型不认标记(calls):
    """`_honors_no_tools` 看的是**把这个 agent 解析成哪个执行器**，不是 cfg 里自报的字段。

    ⚠️ 这条是有意的：`no_tools` 能不能落地取决于**执行器类**，cfg 里写什么都不算数
    （写个 `"honors_no_tools": True` 并不能让 claude-cli 真把工具撤掉）。
    """
    assert pd._honors_no_tools({"type": "ok"}) is True
    assert pd._honors_no_tools({"type": "bad", "honors_no_tools": True}) is False, \
        "cfg 里自报 True 不该被采信 —— 该看执行器类"
    assert pd._honors_no_tools({"type": "根本不存在"}) is False, \
        "解析不出执行器 = 落不了地，按 False 处理（保守方向）"


def test_合成步骤_禁不掉就不合成_退回第一份产出(monkeypatch):
    """委员会合成那一步：`synthesizer = chain[0]` 禁不掉工具 ⇒ **不合成**，退回第一份产出。

    ⚠️ **必须造 3 个席位**。只有两个的话，坏的那个被踢掉后 `outputs` 只剩 1 份，
    函数会在**更早**的 `if len(outputs) == 1` 就 return —— 根本走不到合成那步，
    于是这条用例**测不到要测的东西**（第一版就是这么写的，变异验证时才发现它没红）。
    """
    import singularity.scheduler.execution_judge as ej
    monkeypatch.setattr(pd, "_build_synthesis_prompt", lambda task, outs: "合成提示")
    monkeypatch.setattr(pd, "_committee_allowed", lambda *a, **k: True)
    # ⚠️ `_is_architecture_task` 是函数**内部**延迟导入的，只能打在源模块上。
    monkeypatch.setattr(ej, "_is_architecture_task", lambda t: False)   # 不走 fusion
    monkeypatch.setattr(pd, "_WAVE_TIMEOUT", 5)
    monkeypatch.setattr(pd, "pick_agent_fallback_chain",
                        lambda *a, **k: [{"type": "bad", "model": "bad-1"},
                                         {"type": "ok", "model": "ok-1"},
                                         {"type": "ok", "model": "ok-2"}])
    monkeypatch.setattr(pd, "_prefer_by_strengths", lambda t, c: c)
    monkeypatch.setattr(pd, "_log_arm_event", lambda *a, **k: None)
    monkeypatch.setattr(pd, "_solo_tokens_budget", lambda: 0)
    monkeypatch.setattr(pd, "_EXECUTOR_BY_TYPE",
                        {"ok": _CanHonor, "bad": _CannotHonor})
    monkeypatch.setattr(pd, "_ensure_agent_type", lambda c: c)
    seen = []
    monkeypatch.setattr(pd, "_run_executor",
                        lambda cls, cfg, *a, **k: (seen.append((cls.__name__, cfg.get("tag") or k.get("tag") or a[0] if a else None)),
                                                   ExecutorResult(success=True, raw_output="稿"))[1])

    out = pd.dispatch("设计一个系统", "any", "tid", {}, allow_committee=True)
    assert "_CannotHonor" not in [c for c, _ in seen], \
        f"禁不掉工具的席位居然跑了（它是 chain[0]，也就是合成那步）：{seen}"
    assert len([c for c, _ in seen]) >= 2, f"另外两个能禁的席位得真跑，才走得到合成：{seen}"
    assert out.executor_result.raw_output == "稿", "合成被跳过时该退回第一份产出，不是空手"


def test_单模型fallback链_禁不掉就换下一个(monkeypatch):
    """**09-11 事故真正发生的那条路**：调研/架构的 `dispatch(no_tools=True)`。

    链上第一个禁不掉 ⇒ 跳过它试下一个；**一个都不行就是"无可用 agent"**（fail-closed），
    比"跑了但没禁住"强。
    """
    seen = []
    monkeypatch.setattr(pd, "_EXECUTOR_BY_TYPE",
                        {"ok": _CanHonor, "bad": _CannotHonor})
    monkeypatch.setattr(pd, "_ensure_agent_type", lambda c: c)
    monkeypatch.setattr(pd, "_run_executor",
                        lambda cls, *a, **k: (seen.append(cls.__name__),
                                              ExecutorResult(success=True, raw_output="报告"))[1])
    monkeypatch.setattr(pd, "pick_agent_fallback_chain",
                        lambda *a, **k: [{"type": "bad", "model": "bad-1"},
                                         {"type": "ok", "model": "ok-1"}])
    monkeypatch.setattr(pd, "_prefer_by_strengths", lambda t, c: c)
    monkeypatch.setattr(pd, "_committee_allowed", lambda *a, **k: False)
    monkeypatch.setattr(pd, "_solo_tokens_budget", lambda: 0)

    out = pd.dispatch("调研一下", "any", "tid", {}, no_tools=True)
    assert "_CannotHonor" not in seen, f"禁不掉工具的执行器在 no_tools 阶段居然跑了：{seen}"
    assert seen == ["_CanHonor"], f"该跳过坏的、用好的；实际 {seen}"
    assert out.executor_result.raw_output == "报告"


def test_单模型fallback链_全禁不掉_报无可用而不是带着工具跑(monkeypatch):
    """**对照的另一半**：链上全是禁不掉的 ⇒ 抛"无可用 agent"。

    这是**有意的 fail-closed**：宁可这个阶段明确失败，也不要"跑了、但禁令没落地、
    然后它在项目仓里实现了一整个项目"（09-11）。
    """
    seen = []
    monkeypatch.setattr(pd, "_EXECUTOR_BY_TYPE", {"bad": _CannotHonor})
    monkeypatch.setattr(pd, "_ensure_agent_type", lambda c: c)
    monkeypatch.setattr(pd, "_run_executor",
                        lambda cls, *a, **k: (seen.append(cls.__name__),
                                              ExecutorResult(success=True, raw_output="x"))[1])
    monkeypatch.setattr(pd, "pick_agent_fallback_chain",
                        lambda *a, **k: [{"type": "bad", "model": "bad-1"}])
    monkeypatch.setattr(pd, "_prefer_by_strengths", lambda t, c: c)
    monkeypatch.setattr(pd, "_committee_allowed", lambda *a, **k: False)
    monkeypatch.setattr(pd, "_solo_tokens_budget", lambda: 0)

    # ⚠️ 报文的包装是既有的 `f"{level} 层所有 agent 均失败: {last_error}"` ——
    # 严格说这些 agent 是**被拒**不是"失败"，但换措辞会动到别的用例钉住的串。
    # 能读出来就好：`last_error` 自己写着"禁不掉工具（no_tools 前提不成立）"。
    with pytest.raises(RuntimeError, match="禁不掉工具"):
        pd.dispatch("调研一下", "any", "tid", {}, no_tools=True)
    assert seen == [], "全都禁不掉却还是调了"
