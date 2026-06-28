"""Router tests — 两档后只测 task_type 检测, 不测层级。"""
from singularity.scheduler.router import route
from singularity.scheduler import dispatcher


class TestRouter:
    """任务类型检测。"""

    def test_route_basic(self):
        r = route("fix a typo in README")
        assert r.task_type is not None
        assert isinstance(r.gate_required, bool)

    def test_route_complex(self):
        r = route("重构整个认证系统，支持OAuth2和JWT，改动涉及10个文件")
        assert r.task_type is not None


class TestPropertyRouter:
    """路由不变量。"""

    def test_escalate_monotonic(self):
        # 两档后 escalate 返回 None (不分级)
        assert dispatcher.escalate("") is None

    def test_route_returns_task_type(self):
        result = route("implement a login feature")
        assert result.task_type in ("default", "bugfix", "feature", "refactor", "docs", "fusion")
