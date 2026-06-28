"""Project state machine + workflow tests."""
from unittest.mock import patch, MagicMock
from singularity.scheduler.project import create, Phase, save, list_all, _path


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

    @classmethod
    def teardown_class(cls):
        for p in list_all():
            if "test_" in p.name:
                try:
                    _path(p.id).unlink()
                except Exception:
                    pass


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
        for tid in list(self.p.task_ids):
            tp = tracker._path(tid)
            if tp.exists():
                tp.unlink()
        pp = _path(self.p.id)
        if pp.exists():
            pp.unlink()

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
            assert t.route_level in ("E", "E+", "D")
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
        self.p.phase = Phase.GATE3
        self.p.confirm_gate(Phase.GATE3, "rejected")
        assert self.p.phase == Phase.PLANNING
        self.p.phase = Phase.GATE2
        self.p.confirm_gate(Phase.GATE2, "rejected")
        assert self.p.phase == Phase.RESEARCHING

    def test_architecture_redo_from_executing(self):
        self.p.phase = Phase.EXECUTING
        self.p.architecture_redo()
        assert self.p.phase == Phase.PLANNING
        assert self.p.architecture is None
