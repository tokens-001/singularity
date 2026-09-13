"""P1 三臂实验的 A 臂：**单个模型 + 等预算多轮自修订**。

要钉住三件事，缺一个这条改动就白做：

1. **接线**：开关打开时 `dispatch()` 真的拐进 A 臂，**不**再走委员会。
   （删掉 `dispatch` 里那个 `if _solo_budget` 分支，本文件必须变红。）
2. **默认关时逐字不变**：开关没设就照旧走委员会 —— A/B 实验的前提是"只翻开关"，
   开关本身要是顺手改了默认行为，两臂比的就不是同一个东西了。
3. **停轮的两条判据**：按 token 停（预算口径），以及 token 拿不到时的**轮数兜底**
   —— 少了兜底那条，"token 恒报 0"会变成死循环烧钱。
"""
import pytest

# ⚠️ 先导 `dispatcher`，顺序不能反：`_dispatch_exec` 第 3 行反向 import 它，
# 而 `dispatcher` 末尾又 `from _dispatch_exec import *`。**第一个**导入
# `_dispatch_exec` 会撞上"模块只初始化了一半"，报 `__all__` 里的名字不存在
# （既有的坑，跟 A 臂无关）。
from singularity.scheduler import dispatcher  # noqa: F401
from singularity.scheduler import _dispatch_exec as dx


# ── 开关本身 ──────────────────────────────────────────────────

def test_budget_unset_means_off(monkeypatch):
    monkeypatch.delenv("QIDIAN_SOLO_TOKENS", raising=False)
    assert dx._solo_tokens_budget() == 0, "没设开关就该是关的（0 = 关）"


@pytest.mark.parametrize("raw,expect", [("500", 500), ("abc", 0), ("-1", 0), ("", 0)])
def test_budget_parsing(monkeypatch, raw, expect):
    monkeypatch.setenv("QIDIAN_SOLO_TOKENS", raw)
    assert dx._solo_tokens_budget() == expect


# ── 接线（本文件的重点）──────────────────────────────────────

_ARCH_TASK = "请给出这个系统的架构方案：模块划分与技术栈选型。"


def _stub_chain(monkeypatch):
    """把选链换掉 —— 被测的是**分叉**，不是选链。"""
    fake = [{"model": "m-a", "type": "openai-agent", "api_key_env": "K"},
            {"model": "m-b", "type": "openai-agent", "api_key_env": "K"}]
    monkeypatch.setattr(dx, "pick_agent_fallback_chain", lambda *a, **k: list(fake))
    return fake


def test_dispatch_takes_solo_arm_when_switch_on(monkeypatch):
    """开关打开 ⇒ 走 A 臂，且**不**碰委员会。"""
    _stub_chain(monkeypatch)
    monkeypatch.setenv("QIDIAN_SOLO_TOKENS", "1000")
    called = {"solo": 0, "committee": 0}

    def fake_run_no_tools(cfg, prompt, tag, level, baseline_ref="", cwd=""):
        called["solo"] += 1
        return ("稿子", 10, 0.1)

    monkeypatch.setattr(dx, "_run_no_tools", fake_run_no_tools)
    monkeypatch.setattr(dx, "_dispatch_committee",
                        lambda *a, **k: called.__setitem__("committee", called["committee"] + 1))

    dx.dispatch(_ARCH_TASK, "planning", "t1", {}, restrict_to_lineup=True)

    assert called["solo"] > 0, "开关开着却没走 A 臂 —— 分叉没接上"
    assert called["committee"] == 0, "两臂都跑了 ⇒ A/B 对照不成立"


def test_dispatch_unchanged_when_switch_off(monkeypatch):
    """默认关 ⇒ 照旧走委员会（不许顺手改默认行为）。"""
    _stub_chain(monkeypatch)
    monkeypatch.delenv("QIDIAN_SOLO_TOKENS", raising=False)
    called = {"solo": 0, "committee": 0}

    monkeypatch.setattr(dx, "_run_no_tools",
                        lambda *a, **k: called.__setitem__("solo", called["solo"] + 1))
    monkeypatch.setattr(dx, "_dispatch_committee",
                        lambda *a, **k: called.__setitem__("committee", called["committee"] + 1))

    dx.dispatch(_ARCH_TASK, "planning", "t1", {}, restrict_to_lineup=True)

    assert called["committee"] == 1, "开关没设却改了默认路径 ⇒ 两臂比的不是同一个东西"
    assert called["solo"] == 0


# ── 停轮的两条判据 ───────────────────────────────────────────

def _chain_one():
    return [{"model": "m-a", "type": "openai-agent", "api_key_env": "K"}]


def test_stops_when_token_budget_spent(monkeypatch):
    """每轮 60 token、预算 100 ⇒ 两轮就停（不是跑满轮数）。"""
    rounds = []

    def fake(cfg, prompt, tag, level, baseline_ref="", cwd=""):
        rounds.append(prompt)
        return (f"稿子{len(rounds)}", 60, 0.5)

    monkeypatch.setattr(dx, "_run_no_tools", fake)
    r = dx._dispatch_solo(_ARCH_TASK, "planning", "t1", _chain_one(), 100)

    assert len(rounds) == 2, f"预算 100 / 每轮 60 应停在第 2 轮，实际 {len(rounds)}"
    assert "稿子1" in rounds[1], "第 2 轮没把上一轮的稿喂回去 ⇒ 那不是自修订，是重跑"
    er = r.executor_result
    assert er.token_count == 120
    # 记账契约：按**真实模型名**逐轮记。缺了它 _record_phase_usage 算不出钱。
    assert [u["model"] for u in er.member_usage] == ["m-a", "m-a"]
    assert [u["tokens"] for u in er.member_usage] == [60, 60]


def test_round_cap_saves_us_from_zero_tokens(monkeypatch):
    """拿不到 usage 时 token 恒报 0 ⇒ 光靠预算停不住，必须靠轮数兜底。"""
    n = []

    def fake(cfg, prompt, tag, level, baseline_ref="", cwd=""):
        n.append(1)
        assert len(n) <= 20, "轮数兜底没生效 —— 按 token 停不住时会一直跑（烧钱）"
        return ("稿子", 0, 0.1)

    monkeypatch.setattr(dx, "_run_no_tools", fake)
    dx._dispatch_solo(_ARCH_TASK, "planning", "t1", _chain_one(), 100)

    assert len(n) == dx._SOLO_MAX_ROUNDS


def test_no_output_raises(monkeypatch):
    """一轮都没产出 ⇒ 抛错（别把空稿当成果交上去）。"""
    monkeypatch.setattr(dx, "_run_no_tools", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        dx._dispatch_solo(_ARCH_TASK, "planning", "t1", _chain_one(), 100)
