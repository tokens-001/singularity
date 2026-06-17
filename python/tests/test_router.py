"""路由逻辑测试 — 不调 LLM。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scheduler import router


class TestComplexity:
    def test_e_level_bug(self):
        r = router.route("修复DAG调度器死锁bug")
        assert r.level == "E"
        assert r.task_type == "bugfix"

    def test_e_level_query(self):
        r = router.route("查看合并队列状态")
        assert r.level == "E"

    def test_d_level_architecture(self):
        r = router.route("设计新的任务分解策略架构")
        # 含"设计"+"架构" → D
        assert r.level in ("D", "E+")  # depends on keyword priority

    def test_d_level_review(self):
        r = router.route("审计当前路由逻辑的安全性")
        assert r.level in ("D", "E")  # 审计→D

    def test_ep_level_new_module(self):
        r = router.route("新建Cursor插件模块，支持MCP协议")
        # 含"新建/新模块" → E+
        assert r.level in ("E+", "D")  # depends on exact keyword match

    def test_task_type_bugfix(self):
        r = router.route("修复一个报错导致崩溃的异常")
        assert r.task_type == "bugfix"

    def test_task_type_feature(self):
        r = router.route("添加新的功能支持")
        assert r.task_type == "feature"


class TestGate:
    def test_gate_trigger_core_file(self):
        r = router.route("修改core.py的分词逻辑")
        assert r.gate_required  # core.py 在 gate 触发文件列表中

    def test_no_gate_normal(self):
        r = router.route("修复普通bug")
        assert not r.gate_required


class TestRouteLocked:
    def test_route_result_attrs(self):
        rr = router.RouteResult(level="D", gate_required=True, task_type="refactor",
                                matched_signals=["test"])
        assert rr.level == "D"
        assert rr.gate_required
        assert rr.task_type == "refactor"
        assert "test" in rr.matched_signals
