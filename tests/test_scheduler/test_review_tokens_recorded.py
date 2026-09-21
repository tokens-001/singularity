"""审查花的钱要进账（2026-09-21 补的洞）。

## 洞是什么

`dispatch()` **自己不记账**，记账的一直是调用方：执行层是 `_task_runner`、
规划层是 `workflow._record_phase_usage`。而**审查这几路（crossover / 多模型审查 /
QA 约束验收 / 安全审计）一个都没记** ⇒ 审查花的钱**从来没进过 token 账**。

⚠️ 所以这不是"归不到任务上"，是**压根没记**：
`.qidian` 的十日花费（≈$11.36）是**低估**，而"审查在注定被丢的代码上花了多少"
这个问题**连数据来源都没有**。

## 改法

`validator._review_dispatch` —— 和 `_disp.dispatch` 一模一样，只是顺手把
`executor_result.token_count` 记成一条 `level="review"`。四处审查调用都走它。

## 判据

- **行为**：走真链路（`security_audit_review` → 包装 → 记账）能记出 `level="review"`；
- **结构**：`validator.py` 里 `_disp.dispatch(` **只许出现一次**（就在包装函数里）——
  这一条管的是"四处有没有漏改一处"，行为用例管不了这个。

⚠️ **还没做的**：记的是 `level="review"` + 真实模型名，**不带 project_id/task_id**
⇒ 能答"审查一共花了多少"，还答不了"哪一轮/哪个任务的审查浪费了"。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import dispatcher as disp_mod      # noqa: E402
from singularity.scheduler import validator as val_mod        # noqa: E402

_AGENT = {"model": "fake-model", "type": "openai-agent"}


class _FakeExec:
    def __init__(self, raw, tokens):
        self.raw_output = raw
        self.token_count = tokens


class _FakeResult:
    """形状对着真 `DispatchResult`：`agent_cfg` + `executor_result`。"""

    def __init__(self, raw, tokens):
        self.executor_result = _FakeExec(raw, tokens)
        self.agent_cfg = dict(_AGENT)


@pytest.fixture
def 记账出口(monkeypatch):
    """接住 `record_system_tokens`。

    ⚠️ 必须 patch **`_token_budget` 模块上**那个名字：调用方是
    `from ._token_budget import record_system_tokens`（函数内导入），
    patch validator 的命名空间没用。
    """
    from singularity.scheduler import _token_budget as tb
    seen: list[dict] = []
    monkeypatch.setattr(tb, "record_system_tokens", lambda **k: seen.append(k))
    return seen


def _装桩(monkeypatch, raw, tokens):
    monkeypatch.setattr(disp_mod, "dispatch", lambda *a, **k: _FakeResult(raw, tokens))
    monkeypatch.setattr(disp_mod, "load_agents", lambda *a, **k: {"any": [_AGENT]})
    monkeypatch.setattr(disp_mod, "_all_agents_list", lambda agents: [_AGENT])
    monkeypatch.setattr(disp_mod, "agent_api_available", lambda a: True)


# ═══════════════ 行为：真链路上要记出来 ═══════════════

def test_安全审计花的钱要进账(monkeypatch, 记账出口):
    """真链路：`security_audit_review` → 包装 → 记账。

    变异：把 `_review_dispatch` 里那句 `_record_review_tokens(result)` 删掉 ⇒ 本条红。
    """
    _装桩(monkeypatch, '{"summary":{"verdict":"clean"},"findings":[]}', tokens=1234)

    val_mod.security_audit_review("diff", "/tmp")

    assert 记账出口 == [{"model": "fake-model", "level": "review", "tokens": 1234}], \
        f"审查花了 1234 个 token 却没进账 —— 账就是一整块看不见的成本：{记账出口}"


def test_QA验收花的钱也要进账(monkeypatch, 记账出口):
    """**第二条路**：四处审查都得记，别只接一处（同族：改了三个漏一个）。"""
    _装桩(monkeypatch, '{"summary":{"verdict":"accepted"},"verification":[]}', tokens=77)

    val_mod.qa_acceptance_review(["不许改 auth.py"], "diff", "/tmp")

    assert 记账出口 == [{"model": "fake-model", "level": "review", "tokens": 77}], \
        f"QA 约束验收那条路没记账：{记账出口}"


# ═══════════════ 边界 ═══════════════

def test_没拿到token时不记空账(monkeypatch, 记账出口):
    """拿不到 usage（0）时**别记一条 0** —— 那只会把账塞满噪声。

    同族：`record_tokens` 自己也有 `if tokens > 0` 那道闸。
    """
    _装桩(monkeypatch, '{"summary":{"verdict":"clean"},"findings":[]}', tokens=0)

    val_mod.security_audit_review("diff", "/tmp")

    assert 记账出口 == [], f"token 为 0 也记了一条：{记账出口}"


def test_记账自己挂了不许杀掉审查(monkeypatch):
    """🔴 **记账是旁路，不能反过来把审查弄死** —— 审查死了 = 门失效，代价大得多。

    变异：把 `_record_review_tokens` 里那个 `try` 去掉 ⇒ 本条红（异常穿出去）。
    """
    from singularity.scheduler import _token_budget as tb
    def _炸(**k):
        raise RuntimeError("盘写不进去")
    monkeypatch.setattr(tb, "record_system_tokens", _炸)
    _装桩(monkeypatch, '{"summary":{"verdict":"clean"},"findings":[]}', tokens=5)

    out = val_mod.security_audit_review("diff", "/tmp")   # 不许抛

    assert out["verdict"] == "clean", "记账挂了把审查也带下去了"


# ═══════════════ 结构：四处别漏改一处 ═══════════════

def test_审查调用只有包装函数碰_dispatch():
    """**结构判据**：`validator.py` 里 `_disp.dispatch(` **只许出现一次** —— 就在
    `_review_dispatch` 的函数体里。四处审查调用一旦有人退回裸 `_disp.dispatch`，
    那条路就**静默不记账**（而行为用例只覆盖了其中两条）。

    ⚠️ 这条查的是"**有没有漏一处**"，查不出"记账写错了" —— 那是上面行为用例的活。
    """
    src = Path(val_mod.__file__).read_text(encoding="utf-8")
    n = src.count("_disp.dispatch(")
    assert n == 1, (
        f"`_disp.dispatch(` 在 validator.py 里出现了 {n} 次（只该有包装函数里那 1 次）"
        " —— 多出来的那条路绕过了记账")
