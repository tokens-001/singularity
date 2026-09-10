"""架构阶段的用量要进账（2026-09-11 审计）。

`_dispatch_exec` 里委员会产物被包成手写的 `_FusionResult`，
`token_count = 0` / `elapsed = 0.0` 是**硬编码**的；而 `_task_runner._archive_task_outcome`
正是读这两个字段、`record_tokens` 又有 `if tokens > 0` 闸 ——
于是**整条流水线最贵的一段**（委员会 + 融合）一条用量都不进账，成本统计永远对不上。

根因是 `_run_no_tools` 只回 raw_output、把用量丢了。改成回 `(raw, tokens, elapsed)`，
调用方聚合后回填到 `_FusionResult`。

**在旧代码上会红、且红得对**（断言失败）：旧代码 `_run_no_tools` 返回字符串，
下面这些 stub 的元组会被解包失败吞掉，`token_count` 恒为 0。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import config as cfg              # noqa: E402
# 必须先 import dispatcher: 它末尾 `from _dispatch_exec import *`，
# 直接先 import _dispatch_exec 会拿到 partially-initialized 的模块（仓库既有设计）。
import singularity.scheduler.dispatcher as _d               # noqa: E402,F401
from singularity.scheduler import _dispatch_exec as de       # noqa: E402
from singularity.scheduler import execution_judge as ej      # noqa: E402


@pytest.fixture
def committee(tmp_path, monkeypatch):
    """把委员会的三件外部依赖全打桩：模型调用 / 架构判定 / 融合。"""
    monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(de.witness, "warn", lambda *a, **k: None)
    monkeypatch.setattr(ej, "_is_architecture_task", lambda t: True)
    monkeypatch.setattr(ej, "fuse_architecture_v2",
                        lambda *a, **k: '{"architecture":"fused"}')

    def _stub(tokens, elapsed):
        monkeypatch.setattr(de, "_run_no_tools",
                            lambda c, p, tag, level, baseline_ref="", cwd="":
                            ('{"architecture":"' + c.get("model", "?") + '"}', tokens, elapsed))
    return _stub


class TestCommitteeUsageAccounting:
    def test_multi_member_tokens_are_aggregated(self, committee):
        committee(tokens=1234, elapsed=5.5)
        res = de._dispatch_committee("模块划分 数据模型", "any", "tid", {},
                                     [{"model": "m1"}, {"model": "m2"}])
        er = res.executor_result
        assert er.token_count == 2468, f"两成员用量没聚合: {er.token_count}"
        assert er.elapsed == pytest.approx(11.0)

    def test_single_member_branch_also_carries_usage(self, committee):
        committee(tokens=1234, elapsed=5.5)
        res = de._dispatch_committee("模块划分 数据模型", "any", "tid", {}, [{"model": "m1"}])
        er = res.executor_result
        assert er.token_count == 1234, f"单成员分支仍然不记用量: {er.token_count}"
        assert er.elapsed == pytest.approx(5.5)

    def test_fusion_result_keeps_other_contract_fields(self, committee):
        """别为了加用量把 trace 依赖的其它字段弄丢（缺一个就 AttributeError → trace 不落盘）。"""
        committee(tokens=10, elapsed=1.0)
        res = de._dispatch_committee("模块划分 数据模型", "any", "tid", {},
                                     [{"model": "m1"}, {"model": "m2"}])
        er = res.executor_result
        for f in ("raw_output", "success", "error", "changed_files", "patch_path",
                  "token_count", "elapsed", "tool_events", "fusion_meta"):
            assert hasattr(er, f), f"_FusionResult 少了 {f}"


class TestNoToolsContract:
    def test_returns_usage_tuple(self, monkeypatch):
        """_run_no_tools 的契约：回 (raw, tokens, elapsed)。"""
        monkeypatch.setattr(de.witness, "warn", lambda *a, **k: None)

        class _R:
            raw_output, error, token_count, elapsed = "方案", "", 777, 2.5
        monkeypatch.setattr(de, "_run_executor", lambda *a, **k: _R())
        monkeypatch.setattr(de, "_ensure_agent_type", lambda c: c)
        monkeypatch.setattr(de, "_EXECUTOR_BY_TYPE", {"openai-agent": object})

        raw, tokens, elapsed = de._run_no_tools(
            {"model": "m", "type": "openai-agent"}, "p", "tag", "any")
        assert raw == "方案" and tokens == 777 and elapsed == pytest.approx(2.5)
