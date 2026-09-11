"""删模型后 _disabled 里不该留僵尸 —— 前端「已禁用」区会把它显示出来。

症状：智能体页面的「已禁用」里出现模型目录根本没有的名字（kimi-k2.6 / glm-4.7 …），
点它想启用 → 后端回 warning「这条 agent 是空壳，不会被调度」→ 前端当时把 warning 丢了
→ 用户看到"点了没反应"，查不出为什么。

根因：`model_remove` 调 `remove_agent`，而 `remove_agent` 的语义是**停用** ——
它会主动把 model 写进 _disabled。模型都从库里删了，这个标记没有意义。

两条路都堵上了：
  ① 删模型（`model_remove`）→ 顺手 `purge_disabled`
  ② 停用（`remove_agent`）→ **只给模型库里真有的模型留标记**（`_in_model_library`）。
     库里没有的名字留标记既挡不住任何东西（`agents.toml` 是空占位），又会在前端
     冒出一个查无此物的名字。
"""

import json

import pytest

from singularity.scheduler import _api_admin, _dispatch_crud, config, dispatcher

#: 这个文件里用到的模型名。`remove_agent` 只给模型库里真有的模型留 `_disabled` 标记，
#: 所以造标记前得先把它们放进库（对齐真实情形：你只能停用库里有的模型）。
_LIBRARY = ("m1", "keep-me", "drop-me", "ghost", "whatever")


@pytest.fixture(autouse=True)
def _fill_library():
    from singularity.scheduler import model_registry
    # recommended_for 不能为空 —— 空列表是"删除标记"（见 model_registry.load_models:115）
    for m in _LIBRARY:
        model_registry.add_model(m, "deepseek", recommended_for=["定义"])


def _disabled() -> dict:
    p = config.QIDIAN_DIR / "agents_custom.json"
    return json.loads(p.read_text(encoding="utf-8")).get("_disabled", {})


def test_remove_agent_writes_disabled_marker():
    """先钉住既有语义：停用 = 加进 _disabled。（purge 要清的正是它）"""
    _dispatch_crud.remove_agent("", "m1")
    assert "m1" in _disabled().get("any", [])


def test_purge_disabled_clears_marker():
    _dispatch_crud.remove_agent("", "m1")
    assert _dispatch_crud.purge_disabled("m1") is True
    assert _disabled().get("any", []) == []
    # 幂等：再清一次什么都不做
    assert _dispatch_crud.purge_disabled("m1") is False


def test_purge_disabled_clears_every_level():
    """D 层还留着旧数据（_disabled.D = ["deepseek-v4-pro"]），清的时候不能漏。"""
    _dispatch_crud.remove_agent("", "m1")
    _dispatch_crud.remove_agent("D", "m1")
    assert _dispatch_crud.purge_disabled("m1") is True
    assert all("m1" not in (v or []) for v in _disabled().values())


def test_purge_does_not_touch_other_models():
    _dispatch_crud.remove_agent("", "keep-me")
    _dispatch_crud.remove_agent("", "drop-me")
    _dispatch_crud.purge_disabled("drop-me")
    assert _disabled()["any"] == ["keep-me"]


def test_model_remove_leaves_no_ghost():
    """端到端：删一个模型，禁用列表里不该留下它的名字。"""
    _dispatch_crud.remove_agent("", "ghost")
    assert "ghost" in _disabled().get("any", [])

    _api_admin.model_remove("ghost")
    assert "ghost" not in _disabled().get("any", [])


def test_purge_survives_missing_or_broken_disabled():
    """_disabled 缺失 / 不是 dict 时不能炸 —— 这文件是用户手改得动的。"""
    assert _dispatch_crud.purge_disabled("whatever") is False
    (config.QIDIAN_DIR / "agents_custom.json").write_text(
        json.dumps({"_disabled": {"any": []}}), encoding="utf-8")
    assert _dispatch_crud.purge_disabled("whatever") is False


def test_dispatcher_reexports_purge():
    """_api_admin 是通过 dispatcher 调的，漏了 __all__ 就会 AttributeError。"""
    assert hasattr(dispatcher, "purge_disabled")


def test_model_remove_also_clears_phase_models():
    """删模型要同时清「阶段 → 模型」里的引用，道理和 _disabled 一样。"""
    from singularity.scheduler import phase_models
    phase_models.save({"planning": ["keep", "gone"], "extract": ["gone"]})
    _api_admin.model_remove("gone")
    assert phase_models.load() == {"planning": ["keep"]}


def test_model_remove_survives_broken_phase_models():
    """阶段配置坏了不该让删模型失败。"""
    (config.QIDIAN_DIR / "phase_models.json").write_text("{ 坏 json", encoding="utf-8")
    assert _api_admin.model_remove("x")[1] == 200


class TestRemoveAgentOnlyMarksRealModels:
    """停用只在模型库里真有的模型上留标记 —— 否则就是幽灵。

    触发路径：用户启用了一个库里没有的"空壳 agent"（后端会回 warning），
    再点移除 → 旧代码会往 _disabled 里塞一个查无此物的名字。
    """

    def _add_to_library(self, model: str):
        from singularity.scheduler import model_registry
        model_registry.add_model(model, "deepseek", recommended_for=["定义"])

    def test_library_model_gets_the_marker(self):
        self._add_to_library("real-model")
        _dispatch_crud.add_agent(model="real-model")
        _dispatch_crud.remove_agent("", "real-model")
        assert "real-model" in _disabled().get("any", [])

    def test_unknown_model_leaves_no_ghost(self):
        _dispatch_crud.add_agent(model="not-in-library")
        _dispatch_crud.remove_agent("", "not-in-library")
        assert _disabled().get("any", []) == []

    def test_both_paths_still_remove_from_the_roster(self):
        self._add_to_library("real-model")
        for m in ("real-model", "not-in-library"):
            _dispatch_crud.add_agent(model=m)
        for m in ("real-model", "not-in-library"):
            _dispatch_crud.remove_agent("", m)
        roster = [a["model"] for a in _dispatch_crud._load_custom_agents().get("any", [])]
        assert roster == []

    def test_lookup_failure_fails_closed(self, monkeypatch):
        """注册表读挂了 → 当作"库里有"，宁可多留标记也不丢用户意图。"""
        from singularity.scheduler import model_registry
        monkeypatch.setattr(model_registry, "get",
                            lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
        _dispatch_crud.add_agent(model="whatever")
        _dispatch_crud.remove_agent("", "whatever")
        assert "whatever" in _disabled().get("any", [])
