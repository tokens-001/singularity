"""Project state machine + workflow tests."""
from unittest.mock import patch, MagicMock

import pytest

from singularity.scheduler import config
from singularity.scheduler import project as repo_mod
from singularity.scheduler.project import create, Phase, save, list_all, delete, _path


@pytest.fixture(autouse=True)
def _isolated_qidian_dir(tmp_path, monkeypatch):
    """把 QIDIAN_DIR 指到临时目录 —— 这个文件里的测试会**真建项目**。

    以前不隔离：每次跑测试都往生产数据目录 `.qidian/projects/` 写文件，而 teardown
    只 unlink 状态文件、不删 sidecar（`.architecture.md` / `.executable_tasks.json`
    / `.fusion-models.md`），于是攒下了几百个孤儿（实测 199 组）。
    隔离以后这两个问题一起消失，原来那个 teardown_class 也就不需要了。
    """
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)


class TestProjectState:
    """项目状态机。"""

    def test_create_and_phases(self):
        p = create("test_unit", template="product_dev")
        assert p.phase == Phase.TEMPLATE
        assert p.id

    def test_confirm_gate(self):
        p = create("test_gate", template="product_dev")
        p.phase = Phase.GATE1
        p.confirm_gate(Phase.GATE1, "approved")
        assert p.phase == Phase.PLANNING

    def test_reject_gate(self):
        p = create("test_reject", template="product_dev")
        p.phase = Phase.GATE1
        p.confirm_gate(Phase.GATE1, "rejected")
        assert p.phase == Phase.TEMPLATE

    # teardown_class 已删：QIDIAN_DIR 隔离后所有产物落在 tmp_path，pytest 自己清。


class TestProjectWorkflow:
    """项目工作流: phase 流转 + 任务创建落地 + auto/manual gate。"""

    def setup_method(self):
        self.p = create(
            name="test-wf", description="测试工作流: 写一个 hello world",
            scope="test", template="feature", auto_mode=False,
        )
        self.p.architecture = {
            "architecture": "单文件脚本",
            "tasks": [
                {"id": "T1", "title": "写 hello.py", "description": "创建 hello.py",
                 "complexity": "low", "acceptance": "python hello.py 输出 hello world",
                 "estimated_files": ["hello.py"]},
                {"id": "T2", "title": "写测试", "description": "创建 test_hello.py",
                 "complexity": "low", "acceptance": "python -m pytest test_hello.py 通过",
                 "estimated_files": ["test_hello.py"]},
            ],
            "constraints": [{"text": "不用外部依赖", "type": "no_new_deps",
                              "check": "grep -r 'import' hello.py | wc -l <= 2"}],
            "risks": ["无"],
            "test_strategy": "跑 pytest",
        }
        save(self.p)

    def teardown_method(self):
        from singularity.scheduler import tracker
        from singularity.scheduler.project import get_projects_root
        import shutil
        for tid in list(self.p.task_ids):
            tp = tracker._path(tid)
            if tp.exists():
                tp.unlink()
        pp = _path(self.p.id)
        if pp.exists():
            pp.unlink()
        # 删成品目录 (git repo)，否则下次 create("test-wf") 重名校验失败
        shutil.rmtree(get_projects_root() / self.p.name, ignore_errors=True)

    def test_run_execution_creates_real_tasks(self):
        from singularity.scheduler.workflow import _run_execution
        from singularity.scheduler import tracker
        _run_execution(self.p, {})
        assert len(self.p.task_ids) > 0
        for tid in self.p.task_ids:
            t = tracker.read_task(tid)
            assert t is not None, f"task {tid[:8]} should exist on disk"
            assert t.route_locked
            assert t.project_id == self.p.id
            assert t.route_level == "any"  # 两档后统一 any
        assert self.p.phase.value == "executing"

    def test_run_phase_manual_stops_at_gates(self):
        from singularity.scheduler.workflow import run_phase
        self.p.phase = self.p.phase.__class__.TEMPLATE
        msg = run_phase(self.p, {})
        assert "等待 Owner" in msg
        self.p.phase = self.p.phase.__class__.GATE1
        msg = run_phase(self.p, {})
        assert "等待 Owner gate1" in msg
        assert self.p.phase.value == "gate1"

    def test_run_phase_auto_chains_to_executing(self):
        from singularity.scheduler.workflow import run_phase
        self.p.auto_mode = True
        self.p.phase = self.p.phase.__class__.GATE1
        mock_result = MagicMock()
        mock_result.executor_result.raw_output = '{"architecture":"x","tasks":[{"id":"T1","title":"t","description":"d","complexity":"low","acceptance":"a","estimated_files":["f.py"]}],"constraints":[],"risks":[],"test_strategy":"x"}'
        mock_result.agent_cfg = {"model": "test"}
        with patch("singularity.scheduler.workflow.disp_mod.dispatch", return_value=mock_result):
            msg = run_phase(self.p, {})
        assert "auto: gate1 → planning" in msg
        assert "auto: gate2 → executing" in msg
        assert self.p.phase.value in ("executing", "gate3")

    def test_gate_confirm_approved_advances(self):
        tests = [
            (Phase.GATE1, Phase.PLANNING),
            (Phase.GATE2, Phase.EXECUTING),
            (Phase.GATE3, Phase.DELIVERING),  # S1: GATE3→交付打包→DONE
        ]
        for gate, expected in tests:
            self.p.phase = gate
            self.p.confirm_gate(gate, "approved")
            assert self.p.phase == expected

    def test_gate_confirm_rejected_falls_back(self):
        # S7: GATE3 不在 _REJECT_FALLBACK — 路由由 workflow.handle_gate3_reject 外部处理
        self.p.phase = Phase.GATE3
        result = self.p.confirm_gate(Phase.GATE3, "rejected")
        assert result is None  # GATE3 拒绝不再自动回退
        self.p.phase = Phase.GATE2
        self.p.confirm_gate(Phase.GATE2, "rejected")
        assert self.p.phase == Phase.RESEARCHING

    def test_architecture_redo_from_executing(self):
        self.p.phase = Phase.EXECUTING
        self.p.architecture_redo()
        assert self.p.phase == Phase.PLANNING
        assert self.p.architecture is None


class TestProjectDelete:
    """删除项目必须删干净 —— 残留 <id>/ 空目录会被 repo_dir() 兜底分支当活项目重建。"""

    def test_delete_removes_json_and_dir(self):
        p = create("test_del_clean", template="product_dev")
        d = config.QIDIAN_DIR / "projects" / p.id
        d.mkdir(parents=True, exist_ok=True)
        (d / "repo").mkdir()
        assert d.exists() and _path(p.id).exists()

        assert delete(p.id) is True
        assert not _path(p.id).exists()
        assert not d.exists(), "删项目后残留空目录 → repo_dir() 兜底会把项目'养'回来"

    def test_delete_missing_project_is_false(self):
        assert delete("9999999999999") is False

    def test_delete_handles_readonly_agent_output(self):
        """agent 产出常带 0555/0444 —— 裸 rmtree 会 PermissionError 并把残骸留在原地。"""
        import os
        p = create("test_del_ro", template="product_dev")
        d = config.QIDIAN_DIR / "projects" / p.id
        (d / "sub").mkdir(parents=True)
        f = d / "sub" / "f.txt"
        f.write_text("x")
        os.chmod(f, 0o444)
        os.chmod(d / "sub", 0o555)

        assert delete(p.id) is True
        assert not d.exists(), "只读子目录让删除留下残骸"

    def test_repo_dir_does_not_resurrect_deleted_project(self):
        """复现 2026-09-11 的"项目文件会消失": 删完项目后再碰它, 空目录被 mkdir 养回来,
        load() 仍返 None 而目录在 → 现场看起来像文件自己没了。兜底分支不许再建目录。"""
        p = create("test_del_resurrect", template="product_dev")
        pid = p.id
        assert delete(pid) is True

        repo_mod.repo_dir(pid)  # 任何后续动作(重试/probe/调度循环)都会走到这

        assert not (config.QIDIAN_DIR / "projects" / pid).exists(), \
            "兜底分支把已删项目的空目录重建了 → 症状复现"
