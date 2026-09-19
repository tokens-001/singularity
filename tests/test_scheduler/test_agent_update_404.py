"""`PUT /api/agents/<level>/<model>` 对不存在的模型以前回 **500 + HTML**。

根因：`update_agent` 找不到模型时 `raise RuntimeError(f"Agent {model} 不在 {level} 层")`
—— 没人接，一路冒到 Flask ⇒ 500 + 一页 HTML。前端 `request()` 拿到的不是 JSON，
报错信息也丢了（`docs/OPEN` 里记的那条「该是 400/404」）。

⚠️ **还有半个动作落盘**：`disabled` 分支在抛之前**已经**把标记写进
`agents_custom.json` 的 `_disabled` 了 —— 请求失败了，可盘上多了一条"这个模型被停用"，
而它根本没被启用过。所以修法是**边界先挡**，不是给异常加个 try/except 翻译
（那会被静默-except 棘轮判成"既不出声也不上抛"）。
"""

import json

import pytest

from singularity.scheduler import _api_admin, _dispatch_crud, config, dispatcher


def _custom() -> dict:
    p = config.QIDIAN_DIR / "agents_custom.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


@pytest.fixture
def _one_agent():
    _dispatch_crud.add_agent(model="m1")
    return "m1"


def test_agent_models_in_lists_the_roster(_one_agent):
    assert "m1" in _dispatch_crud.agent_models_in("any")
    assert _dispatch_crud.agent_models_in("nope") == []


def test_unknown_model_is_404_not_500():
    """这条是本次的判据本身 —— 以前这里是未捕获的 RuntimeError。"""
    body, code = _api_admin.agent_update("any", "从来没见过", {"max_turns": 3})
    assert code == 404, f"不在该层的模型该回 404，实际 {code}"
    assert body["ok"] is False and "不在" in body["error"]


def test_unknown_model_does_not_half_write_the_disabled_marker():
    """🔴 **半动作**：404 之前别留下 `_disabled` 标记。

    旧路径是先写标记、再在末尾抛 —— 所以这不是"多一条日志"，是**状态已经被改了**。
    """
    _api_admin.agent_update("any", "从来没见过", {"disabled": True})
    assert _disabled_any() == [], "请求失败了，禁用标记却落盘了"


def test_known_model_still_updates(_one_agent):
    body, code = _api_admin.agent_update("any", "m1", {"max_turns": 7})
    assert code == 200, body
    assert body["agent"]["max_turns"] == 7
    assert _custom()["any"][0]["max_turns"] == 7, "200 了但盘上没变"


def test_disable_then_reenable_round_trips(_one_agent):
    """边界别修过头：**层里真有的**模型照旧能禁用、能改回来。"""
    assert _api_admin.agent_update("any", "m1", {"disabled": True})[1] == 200
    assert "m1" in _disabled_any()
    assert _api_admin.agent_update("any", "m1", {"disabled": False})[1] == 200
    assert "m1" not in _disabled_any()


def test_dispatcher_reexports_agent_models_in():
    """`_api_admin` 走的是 `dispatcher.agent_models_in` —— 这条路得真解析到那个函数
    （PEP 562 惰性转发，与 `purge_disabled` 那条同一个理由）。"""
    from singularity.scheduler import _dispatch_crud as crud
    assert dispatcher.agent_models_in is crud.agent_models_in


def _disabled_any() -> list:
    return _custom().get("_disabled", {}).get("any", [])
