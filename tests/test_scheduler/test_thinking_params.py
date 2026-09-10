"""思考参数透传。

设计取舍：不维护「哪个模型支持哪个思考参数」的能力表 —— 那张表一定会过期
（GLM-5.2 能关思考、5.3 强制开；kimi-k2.6 能关、k2.7 强制开）。所以配了就传，
不支持由 400 容错自动摘掉。
"""
from singularity.scheduler import _dispatch_crud as crud
from singularity.scheduler.executors.openai_agent import (
    _apply_think_params, _drop_rejected_think_param)


# ── 透传 ────────────────────────────────────────────────

def test_apply_passes_only_whitelisted_keys():
    body = {}
    _apply_think_params(body, {"model": "m", "max_tokens": 100,
                               "thinking": {"type": "disabled"}, "reasoning_effort": "low"})
    assert body == {"thinking": {"type": "disabled"}, "reasoning_effort": "low"}


def test_apply_skips_previously_rejected_keys():
    """body 每轮重建，不记住被拒的键就会每轮再撞一次 400。"""
    body = {}
    _apply_think_params(body, {"thinking": {"type": "disabled"}}, skip={"thinking"})
    assert body == {}


# ── 400 容错 ────────────────────────────────────────────

def test_drop_removes_only_the_named_param():
    body = {"thinking": {"type": "disabled"}, "reasoning_effort": "low"}
    assert _drop_rejected_think_param(body, 'HTTP 400: unknown field "thinking"') == "thinking"
    assert body == {"reasoning_effort": "low"}


def test_drop_ignores_unrelated_400():
    body = {"thinking": {"type": "disabled"}}
    assert _drop_rejected_think_param(body, "HTTP 400: bad temperature") == ""
    assert body == {"thinking": {"type": "disabled"}}   # 没误删


# ── request_template 局部更新 ───────────────────────────

def test_merge_tmpl_keeps_existing_fields():
    """只传 thinking 时不能把 model/max_tokens 冲掉 —— 漏写 model 会让运行时
    setdefault 失效（key 已存在），body 里没有 model，请求直接废掉。"""
    base = {"request_template": {"model": "deepseek-v4-flash", "max_tokens": 131072}}
    got = crud._merge_tmpl(base, {"request_template": {"thinking": {"type": "disabled"}}})
    assert got["request_template"] == {
        "model": "deepseek-v4-flash", "max_tokens": 131072,
        "thinking": {"type": "disabled"}}


def test_merge_tmpl_fills_floor_when_agent_had_none():
    """出厂 agent 没有 request_template，只写 thinking 时必须补上 model/额度。"""
    base = {"model": "glm-5.3-flash"}
    got = crud._merge_tmpl(base, {"request_template": {"reasoning_effort": "low"}})
    tmpl = got["request_template"]
    assert tmpl["model"] == "glm-5.3-flash"
    assert tmpl["max_tokens"] > 0
    assert tmpl["reasoning_effort"] == "low"


def test_merge_tmpl_none_deletes_key():
    """前端「恢复默认」传 null —— merge 语义本身删不掉键，不给这条路就回不去。"""
    base = {"model": "m", "request_template": {"model": "m", "max_tokens": 100,
                                              "reasoning_effort": "low"}}
    got = crud._merge_tmpl(base, {"request_template": {"reasoning_effort": None}})
    assert "reasoning_effort" not in got["request_template"]
    assert got["request_template"]["model"] == "m"      # 其他键没被误删


def test_agent_update_then_list_roundtrip(tmp_path, monkeypatch):
    """端到端：PUT 写进去 → GET 读得回来。前端下拉的回显就靠这条链。"""
    import json
    from singularity.scheduler import config, _api_admin
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    (tmp_path / ".qidian" / "agents_custom.json").write_text(json.dumps(
        {"any": [{"model": "test-model-xyz", "type": "openai-agent", "max_turns": 5}]}))

    _, code = _api_admin.agent_update("any", "test-model-xyz",
                                      {"request_template": {"reasoning_effort": "low"}})
    assert code == 200
    listed, _ = _api_admin.agent_list()
    a = next(x for x in listed["any"] if x["model"] == "test-model-xyz")
    assert a["request_template"]["reasoning_effort"] == "low"
    assert a["request_template"]["model"] == "test-model-xyz"   # floor 补上了


def test_merge_tmpl_noop_without_tmpl_update():
    updates = {"max_turns": 3}
    assert crud._merge_tmpl({}, updates) == updates
    assert "request_template" not in crud._merge_tmpl({}, updates)
