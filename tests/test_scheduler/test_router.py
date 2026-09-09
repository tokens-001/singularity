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


class TestStrengthMatching:
    """strengths 冷启动匹配: 任务关键词 → 能力标签 → 模型重排。"""

    def test_label_sql(self):
        from singularity.scheduler._dispatch_exec import _strength_label_for
        assert _strength_label_for("写一个 SQL 查询统计") == "数据查询"

    def test_label_refactor(self):
        from singularity.scheduler._dispatch_exec import _strength_label_for
        assert _strength_label_for("重构多文件认证系统") == "多文件重构"

    def test_label_none(self):
        from singularity.scheduler._dispatch_exec import _strength_label_for
        assert _strength_label_for("写个 fibonacci") == ""

    def test_prefer_moves_matching_to_front(self, monkeypatch):
        from singularity.scheduler._dispatch_exec import _prefer_by_strengths
        import singularity.scheduler.model_registry as mr

        class _M:
            def __init__(self, strengths): self.strengths = strengths
        monkeypatch.setattr(mr, "get", lambda mid: {"a": _M(["数据查询"]), "b": _M([])}.get(mid))
        chain = [{"model": "b"}, {"model": "a"}]
        assert _prefer_by_strengths("写个 sql 查询", chain)[0]["model"] == "a"

    def test_prefer_no_match_keeps_order(self, monkeypatch):
        from singularity.scheduler._dispatch_exec import _prefer_by_strengths
        import singularity.scheduler.model_registry as mr

        class _M:
            def __init__(self, strengths): self.strengths = strengths
        monkeypatch.setattr(mr, "get", lambda mid: {"a": _M(["数据查询"]), "b": _M([])}.get(mid))
        chain = [{"model": "b"}, {"model": "a"}]
        assert _prefer_by_strengths("写个 fibonacci", chain)[0]["model"] == "b"
