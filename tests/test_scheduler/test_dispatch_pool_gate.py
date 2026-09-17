"""停用 / 从没启用的模型不许被派去干活 —— 闸门在 `dispatcher.agent_api_available`。

## 症状（2026-09-18 真机，正在烧钱时抓的）
用户把 `glm-5.2` / `deepseek-v4-pro` **停用**了（阵容只留 `deepseek-flash` + `glm-5.3-flash`），
但分诊账（`.qidian/llm_calls_slow.jsonl`）里这两个**每 20 来秒一轮**、prompt 6 千来字符、
全被 cap 掐断（`cut: true`）—— **一直在花钱**。

## 00:35 修过一次，没修对层
那次把闸门装在融合侧 `execution_judge._call_model`（`_model_in_active_pool`），
测试绿、变异验过——**钱照烧**：00:35:28 重启后 00:37/00:38 分诊账还在进。
告警坐实了真路：`review_pool_expanded:glm-5.2+deepseek-v4-pro`。

## 根因
审查池里只剩一个审查员（写手 `deepseek-flash` 被 `_pick_reviewers` 剔掉，**剔得对**）
⇒ `_review._expand_review_pool` 从**模型注册表**补人，而它只问 `agent_api_available`
——那个函数只看 provider / key，**完全不看激活池** ⇒ 用户停用的模型被补进审查池，
`multi_model_review` 真去调它。

⇒ 闸门改设在 `dispatcher.is_model_active`（**所有选路的下游**：`pick_agent_fallback_chain`
含 cascade 换人 / `_expand_review_pool` / `multi_model_review` 的 stub 分支全过
`agent_api_available`）。融合侧那份判据改成调它 —— **一个判据，两个方向**。

⚠️ 判据是"**在不在激活池里**"，不是只挡 `_disabled`：注册表里有一批
**从来没人启用过**的模型（kimi-k2.6 / qwen3.7-max / gpt-5.5 …），只挡 `_disabled`
的话 `_expand_review_pool` 照样拿它们去审代码 —— 同样是真花钱。
"""
import json

from singularity.scheduler import config, dispatcher, model_registry


def _seed_api():
    """给 `deepseek` 那家配上 key/url。

    ⚠️ **不配的话这些测试会绿得莫名其妙**：`agent_api_available` 会因为
    "没有可用 API" 返回 False —— 真因是没 key，不是闸门。那样删掉闸门测试照样绿，
    等于什么都没钉住（本仓 §"测试要钉接线" 的原形状）。
    """
    from singularity.scheduler import api_store
    api_store.add(api_id="deepseek", provider="DeepSeek",
                  base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY")


def _register(model: str):
    """放进模型注册表 —— `_expand_review_pool` 正是从这里捞人。"""
    model_registry.add_model(model, "deepseek", recommended_for=["定义"])


def _roster(*models: str, disabled: tuple = ()):
    """直接写阵容（`agents_custom.json`）。"""
    (config.QIDIAN_DIR / "agents_custom.json").write_text(json.dumps({
        "any": [{"model": m, "type": "openai-agent",
                 "api_key_env": "DEEPSEEK_API_KEY"} for m in models],
        "_disabled": {"any": list(disabled)},
    }), encoding="utf-8")


def test_阵容里的模型照常放行():
    """对照组：别把闸门修成"谁都不许调"。"""
    _seed_api()
    _register("in-pool")
    _roster("in-pool")
    assert dispatcher.agent_api_available({"model": "in-pool"}) is True


def test_注册表里有但没启用的模型不可用():
    """真机那条：`glm-5.2` 从阵容摘掉后，注册表里还在 —— 不该还能被调。"""
    _seed_api()
    _register("never-enabled")
    _roster("in-pool")
    assert dispatcher.agent_api_available({"model": "never-enabled"}) is False


def test_停用标记里的模型不可用():
    """用户点过"停用"的那种（阵容里已摘掉、`_disabled` 里留了名字）。"""
    _seed_api()
    _register("dropped")
    _roster("in-pool", disabled=("dropped",))
    assert dispatcher.agent_api_available({"model": "dropped"}) is False


def test_扩池不许拿池外的模型去审代码():
    """**接线测试**（真机烧钱的就是这条）：`_expand_review_pool` 补不到人 ⇒ 返回空。

    真机那条告警是 `review_pool_expanded:glm-5.2+deepseek-v4-pro` ——
    补进来的正是用户停用、而在注册表里还在的两个。闸门装上后这里必须是 `[]`
    （审查退化成单 reviewer，但**出声**：`_review` 会 `witness.warn('single_reviewer')`）。
    """
    from singularity.scheduler import _review
    _seed_api()
    _register("never-enabled")
    _roster("the-writer", "reviewer-a")
    assert _review._expand_review_pool(dispatcher, "the-writer", ["reviewer-a"]) == []


def test_扩池这条路现在是死的_代价钉在这里():
    """⚠️ 这条钉的是**取舍的代价**，不是"期望行为" —— 别把它当绿灯读。

    闸门判"在不在激活池里"，而**池子就是阵容** ⇒ 注册表里的人必然不在池里
    ⇒ `_expand_review_pool` **恒返回空**。真机上有 15 个这种模型
    （kimi-k2.6 / qwen3.7-max / gpt-5.5 / glm-5-turbo …）：用户从没启用过任何一个，
    扩池却会拿它们去审代码 —— **真花钱**。这是停掉它的理由。

    代价：阵容只有 2 个模型时，剔掉写手只剩 1 个审查员，"多模型审查"名不副实。
    想要它成立，正确做法是**在阵容里启用第三个模型**，而不是让系统偷偷从注册表补人。
    要不要退掉这条取舍，得用户拍板（已登记 OPEN.md）。
    """
    from singularity.scheduler import _review
    _seed_api()
    for m in ("never-enabled", "also-never-enabled"):
        _register(m)
    _roster("the-writer", "reviewer-a")
    assert _review._expand_review_pool(dispatcher, "the-writer", ["reviewer-a"]) == [], (
        "扩池又把注册表里的人补进来了 —— 那些模型用户从没启用过，是真花钱")


def test_没配阵容不等于停用():
    """⚠️ 池子空 ⇒ 放行。那是"根本没配 agent"（测试环境就是），不是"被停用"。

    混淆两者的代价实测过：`_model_in_active_pool` 加这条时**一次红了 7 个用例**，
    全是没摆池子的。
    """
    _seed_api()
    _register("whatever")
    assert dispatcher.is_model_active("whatever") is True


def test_池子读不出来要放行_和融合侧相反(monkeypatch):
    """**派发侧 fail-open**（融合侧 fail-closed，见 `_model_in_active_pool`）。

    理由：这里是"选谁去干活"的过滤器，读挂了就整个池子空掉、一个任务都派不出去
    —— 比多跑一个模型严重得多。但**要出声**，否则"池子读不出来"和"这个模型没问题"
    在盘上长得一模一样。
    """
    _seed_api()
    _register("m")

    def _boom():
        raise RuntimeError("池子读不出来")

    monkeypatch.setattr(dispatcher, "load_agents", _boom)
    warns = []
    monkeypatch.setattr(dispatcher.witness, "warn", lambda s, m, **k: warns.append(m))
    assert dispatcher.agent_api_available({"model": "m"}) is True
    assert any("pool_check_failed" in w for w in warns), f"静默吞了：{warns}"


def test_融合侧和派发侧共用同一个判据(monkeypatch):
    """**"闸门只有一个"钉在这里**：fusion 不再自带一份池子判据。

    自带一份的后果真机上发生过 —— 两边分叉成"融合拦得住、派发拦不住"，
    而且两边测试都绿（2026-09-18 00:35）。
    """
    from singularity.scheduler import api_store, execution_judge
    monkeypatch.setattr(api_store, "is_available", lambda _p: True)
    monkeypatch.setattr(dispatcher, "is_model_active", lambda _m: False)
    assert execution_judge._model_in_active_pool("whatever") is False
    monkeypatch.setattr(dispatcher, "is_model_active", lambda _m: True)
    assert execution_judge._model_in_active_pool("whatever") is True
