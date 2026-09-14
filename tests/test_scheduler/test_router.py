"""Router tests — 两档后只测 task_type 检测, 不测层级。"""
from singularity.scheduler.router import route, _parse_classify_reply, _VALID_TASK_TYPES
from singularity.scheduler import dispatcher


class TestClassifyReplyValidation:
    """分类器回复的**类型校验**（2026-09-12 补）。

    以前是 `parsed.get("type", "default")` —— 模型吐个别的写法会被**原样存下**，
    下游 `validator._annotate_unverified` 按字面比 `== "bugfix"` 就全不命中，
    那三条"未验证"标注静默消失。这是本仓最典型的那种坏法。
    """

    def test_合法值原样通过(self):
        for t in _VALID_TASK_TYPES:
            assert _parse_classify_reply(f'{{"type": "{t}"}}').task_type == t

    def test_大小写错要回退(self):
        assert _parse_classify_reply('{"type": "bugFix"}').task_type == "default"

    def test_中文值要回退(self):
        assert _parse_classify_reply('{"type": "修复"}').task_type == "default"

    def test_缺type走默认(self):
        assert _parse_classify_reply('{"gate": true}').task_type == "default"

    def test_没有JSON走默认(self):
        assert _parse_classify_reply("模型今天不想输出 JSON").task_type == "default"

    def test_空输入不炸(self):
        assert _parse_classify_reply("").task_type == "default"

    def test_回退后类型一定合法(self):
        """**契约**：解析出来的 task_type 永远落在合法集合里。"""
        for raw in ('{"type":"BUGFIX"}', '{"type":null}', '{"type":123}',
                    '{"type":["bugfix"]}', '[]', '{}', "乱码"):
            assert _parse_classify_reply(raw).task_type in _VALID_TASK_TYPES, raw

    def test_signals和gate照旧透传(self):
        r = _parse_classify_reply('{"type":"feature","gate":true,"signals":["x","y"]}')
        assert r.task_type == "feature" and r.gate_required is True
        assert r.matched_signals == ["x", "y"]


class TestRouter:
    """任务类型检测。"""

    def test_route_basic(self, monkeypatch):
        """`route()` 要把**分类器的结果原样透出来**。

        ⚠️ 原来只断言 `task_type is not None` / `isinstance(gate_required, bool)` ——
        而 `RouteResult` 的**默认值**恰好就是 `default` / `False`
        ⇒ **把 `return _llm_classify(task)` 整行换成"恒返回默认值"，这三条照样全绿**
        （2026-09-14 变异实测坐实）。那等于没测路由。
        现在钉的是"这一跳真的把分类结果传出来了"。
        """
        from singularity.scheduler import router as R
        sentinel = R.RouteResult(task_type="refactor", gate_required=True)
        monkeypatch.setattr(R, "_llm_classify", lambda _t: sentinel)
        assert route("fix a typo in README") is sentinel, \
            "route 没把分类器的结果透出来（恒返回默认值也能满足旧断言）"

    def test_route_complex(self, monkeypatch):
        """同上：长描述必须**真的走分类器**，不是返回默认值蒙混。"""
        from singularity.scheduler import router as R
        called: list = []
        sentinel = R.RouteResult(task_type="refactor", gate_required=True)
        monkeypatch.setattr(R, "_llm_classify",
                            lambda t: (called.append(t), sentinel)[1])
        assert route("重构整个认证系统，支持OAuth2和JWT，改动涉及10个文件") is sentinel
        assert called, "长描述没走分类器"


class TestPropertyRouter:
    """路由不变量。"""

    def test_escalate_monotonic(self):
        # 两档后 escalate 返回 None (不分级)
        assert dispatcher.escalate("") is None

    def test_route_returns_task_type(self):
        """**短描述短路成 default**（不调 LLM 那条路），且类型落在词表里。

        ⚠️ 原来断言 `in (六个值)`，而其中就有默认值 `default` ⇒
        "恒返回默认值"也满足它（2026-09-14 变异坐实）。现在把短路那半钉死。
        """
        result = route("改造")                      # 2 字 < 20 ⇒ 短路，不调分类器
        assert result.task_type == "default", "短描述该短路成 default"


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
