"""外层 verdict 的大小写归一 —— fail-open 回归测试（2026-09-11 审计）。

**这个 bug**：生产端 `security_audit_review` / `qa_acceptance_review` 只把**精确小写**
的 `critical` / `rejected` 归一成 `needs_fix`；消费端 `_review.py` 又拿 `== "needs_fix"`
精确比。判官是模型，输出常在大小写/空格上飘（`"Critical"` / `"CRITICAL"` / `" critical "`）。

两头一错，`findings` 被**整条丢弃** → 安全审计报的漏洞不拦、不告警、不进 unverified →
**真漏洞随代码合并**。

这是本仓库修过一轮的同族 bug：`修复归档-20260910.md` #2 修的是 **severity** 字段的
大小写 fail-open，这次漏的是 **verdict** 字段 —— 同一个病换了字段。
（`_review.py` 里的 `_norm()` 早就存在，docstring 写的就是这件事，只是生产端没用它。）

**在旧代码上会红、且红得对**（因断言失败，不是 TypeError）：
`_norm_verdict` 不存在 → 这一条其实是 TypeError；但 `TestConsumerSide` 里
`_norm("Critical") == "needs_fix"` 那类断言在旧代码上也是真的 —— 所以真正锁住
"生产端也必须归一" 的是 `TestProducerSide` 的 `"Critical"` 用例。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import dispatcher as disp_mod      # noqa: E402
from singularity.scheduler import validator as val_mod        # noqa: E402
from singularity.scheduler._review import _norm               # noqa: E402


class _FakeExec:
    def __init__(self, raw):
        self.raw_output = raw


class _FakeResult:
    def __init__(self, raw):
        self.executor_result = _FakeExec(raw)


@pytest.fixture
def fake_dispatch(monkeypatch):
    """拦掉真模型调用。

    两个注意点（都踩过）：
    1. 这两个函数是**函数内** `from . import dispatcher as _disp`，
       所以要 patch dispatcher 模块的属性，patch validator 的命名空间没用。
    2. conftest 会隔离 QIDIAN_DIR → `load_agents()` 读不到 agents_custom.json →
       `model_cfgs` 为空 → 函数在"无可用模型"分支提前返回（不是本测试要测的路径）。
       所以 agent 相关函数一并打桩，让用例自洽、不依赖环境。
    """
    def _install(payload=None, raw=None):
        text = raw if raw is not None else json.dumps(payload)
        monkeypatch.setattr(disp_mod, "dispatch", lambda *a, **k: _FakeResult(text))
        monkeypatch.setattr(disp_mod, "load_agents", lambda *a, **k: {"any": [_AGENT]})
        monkeypatch.setattr(disp_mod, "_all_agents_list", lambda agents: [_AGENT])
        monkeypatch.setattr(disp_mod, "agent_api_available", lambda a: True)
    return _install


_AGENT = {"model": "fake-model", "type": "openai-agent"}


class TestProducerSide:
    """生产端：模型吐什么大小写，出来都得是可消费的 needs_fix。"""

    @pytest.mark.parametrize("raw", ["critical", "Critical", "CRITICAL",
                                     " critical ", "Critical\n", "needs_fix"])
    def test_security_verdict_normalized(self, fake_dispatch, raw):
        fake_dispatch({"summary": {"verdict": raw},
                       "findings": [{"severity": "critical", "cwe": "CWE-1"}]})
        out = val_mod.security_audit_review("diff", "/tmp")
        assert out["verdict"] == "needs_fix", f"{raw!r} 没被归一化 → 漏洞会被放行"

    def test_security_clean_stays_clean(self, fake_dispatch):
        fake_dispatch({"summary": {"verdict": "clean"}, "findings": []})
        assert val_mod.security_audit_review("diff", "/tmp")["verdict"] == "clean"

    @pytest.mark.parametrize("raw", ["rejected", "Rejected", "REJECTED ", "needs_fix"])
    def test_qa_verdict_normalized(self, fake_dispatch, raw):
        fake_dispatch({"summary": {"verdict": raw},
                       "verification": [{"constraint": "x", "status": "fail"}]})
        # 约束清单必须非空，否则 :659 会以"无约束清单"提前返回 accepted
        out = val_mod.qa_acceptance_review(["不许改 auth.py"], "diff", "/tmp")
        assert out["verdict"] == "needs_fix", f"{raw!r} 没被归一化"

    def test_non_json_is_fail_closed(self, fake_dispatch):
        """模型没吐 JSON = 没审成 → 必须 needs_fix，不能标 clean。

        这条本来就是对的（fail-closed），加进来是**锁住别退化** ——
        安全门禁最怕的就是"没审成"被当成"审过且干净"。
        """
        fake_dispatch(raw="模型写了一堆散文，没有任何 JSON")
        assert val_mod.security_audit_review("d", "/tmp")["verdict"] == "needs_fix"


class TestConsumerSide:
    """消费端：`_review.py` 拿 verdict 比 `needs_fix` 前必须归一。"""

    @pytest.mark.parametrize("v", ["needs_fix", "Needs_Fix", "NEEDS_FIX", " needs_fix "])
    def test_norm_matches(self, v):
        assert _norm(v) == "needs_fix"

    def test_none_and_empty_do_not_match(self):
        assert _norm(None) == ""
        assert _norm("") == ""


class TestEndToEnd:
    """生产端 → 消费端 串起来：模型吐 "Critical" 时 findings 不能再被丢掉。"""

    def test_capitalized_critical_reaches_consumer(self, fake_dispatch):
        fake_dispatch({"summary": {"verdict": "Critical"},
                       "findings": [{"severity": "critical", "cwe": "CWE-79"}]})
        sa = val_mod.security_audit_review("diff", "/tmp")
        # 消费端的真实表达式（_review.py 里就是这么写的）
        findings = sa.get("findings", []) if _norm(sa.get("verdict")) == "needs_fix" else []
        assert len(findings) == 1, "模型吐 'Critical' 时 findings 被整条丢弃 → 真漏洞放行"
