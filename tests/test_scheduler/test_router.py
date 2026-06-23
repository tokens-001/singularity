"""Router + escalation tests."""
from singularity.scheduler.router import route
from singularity.scheduler import dispatcher


class TestRouter:
    """路由判定。"""

    def test_route_basic(self):
        r = route("fix a typo in README")
        assert r.level in ("E", "E+", "D")
        assert r.task_type is not None

    def test_route_complex(self):
        r = route("重构整个认证系统，支持OAuth2和JWT，改动涉及10个文件")
        assert r.level in ("E", "E+", "D")


class TestPropertyRouter:
    """路由不变量。"""

    def test_escalate_monotonic(self):
        order = {"E": 1, "E+": 2, "D": 3}
        for lvl in ("E", "E+", "D"):
            nxt = dispatcher.escalate(lvl)
            if nxt:
                assert order.get(nxt, 0) > order.get(lvl, 0)

    def test_route_returns_level_in_hierarchy(self):
        result = route("implement a login feature")
        assert result.level in ("E", "E+", "D")
