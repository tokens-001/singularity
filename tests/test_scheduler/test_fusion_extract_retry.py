"""**停用/欠费的模型不许被真调** —— 闸门设在 `_call_model` 上（2026-09-18 真机）。

## 症状
用户把 `glm-5.2` / `deepseek-v4-pro` **停用**了（阵容里只留 `deepseek-flash`），
但分诊账（`.qidian/llm_calls_slow.jsonl`）里这两个**每 20 来秒出现一次**、
prompt 都 6 千来字符 —— **一直在烧钱**，而且全被 cap 掐断（`cut: true`）。

## 根因
`execution_judge` 里有一条**硬编码**的备选链：
`_V2_EXTRACT_FALLBACKS = ("glm-5.2", "deepseek-v4-pro")`，
提取第一次失败后照单重试 —— 而 `_call_model` 走的 `_resolve_api`
**只看 provider + api_store，完全不看激活池**（本模块 `_disabled` 出现 0 次）
⇒ 用户停用的模型照调不误。**代码自己的注释早就写过这个案例**（`_usable` 那段），
但那是逐点补的，漏了重试池这条口子。

⇒ 闸门改设在 `_call_model`（**所有调用的最后一道**），不再逐点补 ——
补调用点就是 §60 那个形状（同一件事多个入口，补一处漏一处）。
"""
from singularity.scheduler import execution_judge as ej


def _pool(monkeypatch, *models):
    """把 `_model_in_active_pool` 的两关摆好：① 池子里有谁 ② provider 没欠费。"""
    from singularity.scheduler import api_store, dispatcher
    monkeypatch.setattr(dispatcher, "load_agents",
                        lambda: {"any": [{"model": m, "type": "openai-agent"} for m in models]})
    monkeypatch.setattr(api_store, "is_available", lambda _p: True)


def test_停用的模型不许被真调(monkeypatch):
    """闸门：不在池子里 ⇒ 压根不许走到"发请求"那一步。"""
    _pool(monkeypatch, "deepseek-flash")
    reached = []
    monkeypatch.setattr(ej, "_resolve_api",
                        lambda m: (reached.append(m), ("K", "http://x"))[1])
    warns = []
    monkeypatch.setattr(ej.witness, "warn", lambda scope, msg, **k: warns.append(msg))

    out = ej._call_model("hi", "glm-5.2")

    assert out == ""
    assert reached == [], f"停用的模型还是走到发请求了：{reached}"
    assert any("model_not_in_pool" in w for w in warns), (
        f"静默拒绝 ⇒ 调用方只看到「这次提取失败」，而真因是「你要的模型被停用了」：{warns}")


def test_池子里的模型照常放行(monkeypatch):
    """对照组：别把闸门修成"谁都不许调"。"""
    _pool(monkeypatch, "deepseek-flash", "glm-5.2")
    assert ej._model_in_active_pool("deepseek-flash") is True
    assert ej._model_in_active_pool("glm-5.2") is True     # 池里有就放行


def test_provider欠费也不许调(monkeypatch):
    """第二关：模型在池里，但 provider 欠费 —— 同样不许调。"""
    from singularity.scheduler import api_store, dispatcher
    monkeypatch.setattr(dispatcher, "load_agents",
                        lambda: {"any": [{"model": "glm-5.2"}]})
    monkeypatch.setattr(api_store, "is_available", lambda _p: False)
    assert ej._model_in_active_pool("glm-5.2") is False


def test_查不了就判不可用_别fail_open(monkeypatch):
    """⚠️ fail-**closed**：查不了 ⇒ 判不可用。

    方向是刻意的：换兜底的收益只是"选手别给自己出题"（**质量**问题），
    而真调一个停用模型 / 欠费 provider 的代价是**花钱 + 功能可能挂**。
    """
    from singularity.scheduler import dispatcher

    def _boom():
        raise RuntimeError("池子读不出来")

    monkeypatch.setattr(dispatcher, "load_agents", _boom)
    assert ej._model_in_active_pool("glm-5.2") is False
