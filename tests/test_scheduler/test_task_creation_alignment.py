"""建任务的**两条路必须做全套**（2026-09-17 真机坐实，两条都是"另一个入口没做全"）。

## ① 重规划之后，任务得跟着换
`_run_planning` 以前不清 `task_ids`，而两条建任务的路**都够不着"重规划后还有旧任务"**：
  · `_run_execution` —— **GATE2 批准时不跑它**（executing 归调度循环推，刻意如此）；
  · `_decompose_and_create_tasks` —— 被 `if not proj.task_ids:` 挡住，旧任务还在 ⇒ 不触发。
⇒ 新架构的任务**一个都不建**，项目拿着旧任务冲过执行层
  （真机实测：**10 秒**从 `gate2` 冲到 `reviewing`，重规划白花钱）。

## ② `_decompose_and_create_tasks` 得跟 `_run_execution` 一样把上下文带上
它以前**只传 `t["desc"]`**，丢了 `context_snippet`（**约束和机器检查命令就在里面**）
和 `acceptance` ⇒ 干活的人不知道要建哪些测试文件 ⇒ **机器检查 10/10 全红**
（`file or directory not found: tests/…`）。
⚠️ §60 的形状：同一个动作两个入口，一条做全了、一条没做全。
"""
import json

import pytest

# ⚠️ 必须先导 `workflow` —— `_workflow_phases` 是它的辐条，反过来先导会
# `partially initialized module`（本仓的循环导入，今天已踩过一次）。
import singularity.scheduler.workflow  # noqa: F401

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
from singularity.scheduler import tracker
from singularity.scheduler import orchestrator
from singularity.scheduler.project import Phase


def _mk(tmp_path, monkeypatch, *, task_ids=None, arch=None):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    config.ensure_dirs()
    p = proj_mod.ProjectState(
        id="p1", name="探路", description="做个日志工具", raw_constraints=[],
        owner_confirm={}, constraints_checklist=[], task_ids=list(task_ids or []),
        issues=[], supervision_log=[], lineage=[], handoffs=[], agent_lineup={})
    p.phase = Phase.EXECUTING
    p.architecture = arch or {}
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)
    return p


class TestDecomposeCarriesContext:
    """② 建出来的任务描述里，**约束/检查命令/验收标准**都得在。"""

    def _run(self, tmp_path, monkeypatch, item):
        import singularity.scheduler.execution_judge as ej
        p = _mk(tmp_path, monkeypatch, arch={"constraints": [{"rule": "必须零第三方依赖"}]})
        monkeypatch.setattr(ej, "decompose_architecture", lambda _a: [item])
        monkeypatch.setattr(proj_mod, "ensure_repo", lambda _id: None)
        orchestrator._decompose_and_create_tasks(p, {})
        assert p.task_ids, "一个任务都没建出来"
        return tracker.read_task(p.task_ids[0])

    def test_约束和检查命令要进任务描述(self, tmp_path, monkeypatch):
        """`context_snippet` 里装着**约束 + 机器检查命令** —— 干活的人靠它才知道要建哪些测试文件。"""
        t = self._run(tmp_path, monkeypatch, {
            "desc": "写测试: 建 tests/test_cli.py",
            "context_snippet": "约束: 正常路径\n机器检查: python3 -m pytest -q tests/test_cli.py",
            "acceptance": "pytest 全绿",
        })
        d = str(t.description)
        assert "tests/test_cli.py" in d, "检查命令没进描述 —— 干活的不知道要建哪个文件"
        assert "python3 -m pytest" in d, "机器检查命令没进描述"
        assert "验收标准" in d and "pytest 全绿" in d, "验收标准没进描述"

    def test_架构的约束也要带上(self, tmp_path, monkeypatch):
        t = self._run(tmp_path, monkeypatch, {"desc": "x", "acceptance": "y"})
        assert "必须零第三方依赖" in str(t.description), "架构里的约束没进任务描述"

    def test_架构没给_context_时不许硬塞一行假的(self, tmp_path, monkeypatch):
        """边界：没有 snippet 就**不写那一段**，别留一行空的"相关上下文:"。"""
        t = self._run(tmp_path, monkeypatch, {"desc": "x", "acceptance": "y"})
        assert "相关上下文" not in str(t.description)


class TestReplanClearsTaskIds:
    """① 重规划之后 `task_ids` 得清空 —— 否则新架构的任务永远建不出来。"""

    def _run_planning(self, tmp_path, monkeypatch, p):
        from singularity.scheduler import _workflow_phases as wp
        monkeypatch.setattr(proj_mod, "load", lambda _id: p)
        monkeypatch.setattr(proj_mod, "ensure_repo", lambda _id: None)
        monkeypatch.setattr(wp, "_save_phase_output", lambda *a, **k: None)
        monkeypatch.setattr(wp, "_index_phase_memory", lambda *a, **k: None)
        monkeypatch.setattr(wp, "_materialize_test_cases", lambda *a, **k: None)
        monkeypatch.setattr(wp, "_phase_selection", lambda *a, **k: (None, False))
        monkeypatch.setattr(wp, "_should_skip", lambda *a, **k: False)
        monkeypatch.setattr(wp, "_run_budget_gate", lambda *a, **k: "")
        monkeypatch.setattr(wp, "_run_preflight", lambda *a, **k: None)
        monkeypatch.setattr(wp, "_read_phase_output", lambda *a, **k: "")

        class ER:
            raw_output = '{"modules":[{"name":"m"}],"tasks":[{"id":"T1","title":"t","description":"d","complexity":"low","layer":"impl","acceptance":"a"}],"constraints":[{"rule":"r","check":{"argv":["python3","-m","pytest"],"expect_exit":0}}]}'

        class D:
            executor_result = ER()
            agent_cfg = {"model": "probe"}
        monkeypatch.setattr(wp, "_safe_dispatch", lambda *a, **k: (D(), ""))
        wp._run_planning(p, {})
        return p

    def test_重规划必须清空旧task_ids(self, tmp_path, monkeypatch):
        """不清的后果：两条建任务的路都够不着，新架构的任务**一个都不建**。"""
        p = _mk(tmp_path, monkeypatch, task_ids=["old1", "old2", "old3"])
        self._run_planning(tmp_path, monkeypatch, p)
        assert p.task_ids == [], \
            f"重规划后旧 task_ids 还留着 ⇒ 调度循环那条 `if not proj.task_ids` 永远不触发：{p.task_ids}"

    def test_清空之后调度循环那条守卫才会触发(self, tmp_path, monkeypatch):
        """端到端：**走调度循环那个真函数**，确认 `task_ids` 空 ⇒ 它真去拆架构。

        ⚠️ 第一版这条是**假接线**：我自己把守卫那句 `if not proj.task_ids` 手写了一遍
        —— 那是"函数对≠接线通"的标准形状（测的是我的复述，不是生产那条路）。
        改成调 `_auto_trigger_test_fix`（护栏就在它里面）。
        """
        import singularity.scheduler.execution_judge as ej
        p = _mk(tmp_path, monkeypatch, task_ids=["old1"],
                arch={"constraints": [], "tasks": [{"id": "T1", "title": "t", "description": "d"}]})
        monkeypatch.setattr(proj_mod, "save", lambda _x: None)
        # 真函数用它遍历项目 —— 项目没落盘，得喂给它
        monkeypatch.setattr(proj_mod, "list_all", lambda: [p])
        monkeypatch.setattr(proj_mod, "ensure_repo", lambda _id: None)
        monkeypatch.setattr(ej, "decompose_architecture",
                            lambda _a: [{"desc": "新任务", "acceptance": "a"}])

        # ① 旧任务还在 ⇒ **不该**重建（这是原来那个 guard 的意思）
        orchestrator._auto_trigger_test_fix({}, [])
        assert p.task_ids == ["old1"], "旧任务还在却重建了？"

        # ② 重规划把它清掉之后 ⇒ 真函数该重建
        p.task_ids = []
        orchestrator._auto_trigger_test_fix({}, [])
        assert p.task_ids, "清空之后守卫没重建任务 —— 那清空就没意义"

    def test_不清空则永远不重建_这就是那个洞(self, tmp_path, monkeypatch):
        """反面：**不清**的后果 —— 新架构的任务一个都不建（真机就是这么白花钱的）。"""
        import singularity.scheduler.execution_judge as ej
        p = _mk(tmp_path, monkeypatch, task_ids=["old1"],
                arch={"constraints": [], "tasks": [{"id": "T1", "title": "t", "description": "d"}]})
        monkeypatch.setattr(proj_mod, "save", lambda _x: None)
        monkeypatch.setattr(proj_mod, "list_all", lambda: [p])
        called = []
        monkeypatch.setattr(ej, "decompose_architecture",
                            lambda _a: (called.append(1), [{"desc": "新", "acceptance": "a"}])[1])
        orchestrator._auto_trigger_test_fix({}, [])
        assert called == [], "不清 task_ids 时居然也拆了 —— 那这条 fix 的前提就不成立"
