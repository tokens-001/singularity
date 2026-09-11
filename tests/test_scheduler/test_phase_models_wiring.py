"""「阶段 → 模型」接线回归。

两条最要命的：
① **席位真被限制住** —— 旧代码是"lineup 优先 + 其余追加"，光是"指定"限制不住委员会
② **指定顺序不被重排** —— 用户点名主力之后，学习器按历史权重把它顶掉 = 语义撒谎
"""

import types

import pytest

from singularity.scheduler import dispatcher as D


def _agents(*models):
    return {"any": [
        {"model": m, "type": "openai-agent", "entry": "http://x",
         "api_key_env": "K", "roles": ["generic"]}
        for m in models
    ]}


@pytest.fixture(autouse=True)
def _stub_availability(monkeypatch):
    """把"这个模型现在能不能调"打成恒真 —— 测的是排序/限制逻辑，不是 API 探测。"""
    monkeypatch.setattr(D, "agent_api_available", lambda cfg: True)
    from singularity.scheduler import _model_breaker
    monkeypatch.setattr(_model_breaker, "is_available", lambda m: True)


@pytest.fixture
def warns(monkeypatch):
    out = []
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: out.append(a))
    return out


def _fake_learner(weights: dict):
    """假学习器：给定 model → hedge_weight。"""
    stats = {f"default::{m}": types.SimpleNamespace(model=m, hedge_weight=w)
             for m, w in weights.items()}
    return types.SimpleNamespace(_stats=stats)


class TestRestrictToLineup:
    def test_restrict_returns_only_the_lineup(self):
        # 这条就是"委员会席位被限制住"的回归。不修 _collect 的追加行为，这里会多出 c。
        chain = D.pick_agent_fallback_chain(
            _agents("a", "b", "c"), "any",
            project_lineup={"any": ["b"]}, restrict_to_lineup=True)
        assert [a["model"] for a in chain] == ["b"]

    def test_no_restrict_still_appends_the_rest(self):
        # 旧语义必须原样保留：lineup 只是"优先"，其余仍作兜底。
        chain = D.pick_agent_fallback_chain(
            _agents("a", "b", "c"), "any",
            project_lineup={"any": ["b"]})
        assert [a["model"] for a in chain] == ["b", "a", "c"]

    def test_restrict_preserves_the_configured_order(self):
        chain = D.pick_agent_fallback_chain(
            _agents("a", "b", "c"), "any",
            project_lineup={"any": ["c", "a", "b"]}, restrict_to_lineup=True)
        assert [a["model"] for a in chain] == ["c", "a", "b"]

    def test_restrict_skips_the_learner_reorder(self, monkeypatch):
        """指定了主力，学习器不许把它顶掉。

        route_learner.json 里 deepseek-v4-flash 的历史权重是 2.12 —— 真实场景下
        这条路径每次都会触发，不是理论问题。
        """
        from singularity.scheduler import route_learner
        monkeypatch.setattr(route_learner, "load_learner",
                            lambda: _fake_learner({"a": 1.0, "b": 9.9}))
        chain = D.pick_agent_fallback_chain(
            _agents("a", "b"), "any",
            project_lineup={"any": ["a", "b"]}, restrict_to_lineup=True)
        assert [a["model"] for a in chain] == ["a", "b"]

    def test_learner_reorder_still_works_without_restrict(self, monkeypatch):
        """别把功能关死：不受限时学习器排序照旧生效。"""
        from singularity.scheduler import route_learner
        monkeypatch.setattr(route_learner, "load_learner",
                            lambda: _fake_learner({"a": 1.0, "b": 9.9}))
        chain = D.pick_agent_fallback_chain(
            _agents("a", "b"), "any",
            project_lineup={"any": ["a", "b"]})
        assert [a["model"] for a in chain] == ["b", "a"]

    def test_restrict_falls_back_to_full_pool_when_nothing_resolves(self, warns):
        """指定全解析不出来 → fail-open 回全池，但必须留痕。

        静默回全池的话，界面上配了东西却完全没生效，查都没处查。
        """
        chain = D.pick_agent_fallback_chain(
            _agents("a", "b"), "any",
            project_lineup={"any": ["ghost-1", "ghost-2"]}, restrict_to_lineup=True)
        assert [a["model"] for a in chain] == ["a", "b"]
        assert any("lineup_all_unavailable" in str(w) for w in warns)

    def test_partial_resolution_still_restricts(self):
        # 只要解析出 1 个就限制 —— 别因为有个坏名字就把整条退化成全池。
        chain = D.pick_agent_fallback_chain(
            _agents("a", "b"), "any",
            project_lineup={"any": ["a", "ghost"]}, restrict_to_lineup=True)
        assert [a["model"] for a in chain] == ["a"]

    def test_restrict_without_lineup_changes_nothing(self):
        chain = D.pick_agent_fallback_chain(
            _agents("a", "b"), "any", restrict_to_lineup=True)
        assert [a["model"] for a in chain] == ["a", "b"]


class _FakeDisp:
    """_pick_reviewers 要的最小接口。unavailable 里的模型一律判不可用。"""

    def __init__(self, roster, unavailable=()):
        self.roster = roster
        self.unavailable = set(unavailable)

    def _find_agent_by_model(self, agents, m):
        return next((a for a in agents["any"] if a["model"] == m), None)

    def agent_api_available(self, cfg):
        return cfg.get("model") not in self.unavailable


class TestPickReviewers:
    """审查这一项配了名单 → 按名单；没配 / 配废了 → 退回全池减写手取前 2。"""

    def _run(self, roster, writer, monkeypatch, designated=None, unavailable=()):
        from singularity.scheduler import phase_models, _review
        if designated is not None:
            phase_models.save({"reviewing": designated})
        agents = _agents(*roster)
        pool = D._all_agents_list(agents)
        return _review._pick_reviewers(_FakeDisp(roster, unavailable), agents, pool, writer)

    def test_unconfigured_uses_pool_minus_writer(self, monkeypatch):
        designated, picked = self._run(["a", "b", "c"], "a", monkeypatch)
        assert designated == []
        assert picked == ["b", "c"]

    def test_designated_list_wins_and_is_not_truncated_to_two(self, monkeypatch):
        # 旧逻辑只取前 2；配了名单就该全用，否则"配了 3 个审查员"是假的。
        designated, picked = self._run(["a", "b", "c", "d"], "a", monkeypatch,
                                       designated=["b", "c", "d"])
        assert designated == ["b", "c", "d"]
        assert picked == ["b", "c", "d"]

    def test_writer_is_dropped_and_warned(self, monkeypatch, warns):
        designated, picked = self._run(["a", "b", "c"], "a", monkeypatch,
                                       designated=["a", "b"])
        # 自己审自己 = 没有审查，必须剔
        assert picked == ["b"]
        assert any("designated_reviewer_is_writer" in str(w) for w in warns)

    def test_designated_preserves_configured_order(self, monkeypatch):
        _, picked = self._run(["a", "b", "c"], "a", monkeypatch,
                              designated=["c", "b"])
        assert picked == ["c", "b"]

    def test_unavailable_designated_entries_are_skipped(self, monkeypatch):
        _, picked = self._run(["a", "b", "c"], "a", monkeypatch,
                              designated=["b", "c"], unavailable=["b"])
        assert picked == ["c"]

    def test_all_designated_unavailable_falls_back_to_pool(self, monkeypatch, warns):
        # 退回而不是"没人审" —— 配错了不该让审查整层消失
        designated, picked = self._run(["a", "b", "c"], "a", monkeypatch,
                                       designated=["ghost"], unavailable=["ghost"])
        assert designated == []
        assert picked == ["b", "c"]
        assert any("designated_reviewers_all_unavailable" in str(w) for w in warns)


class TestExtractorModel:
    """提取员三级优先级：阶段配置 > fusion.toml > 默认值。"""

    def _pick(self, monkeypatch, fusion: str = ""):
        from singularity.scheduler import execution_judge as ej
        monkeypatch.setattr(ej, "_load_fusion_config",
                            lambda: {"custom": {"extract_model": fusion}} if fusion else {})
        return ej._v2_extractor_model()

    def test_phase_config_beats_fusion_toml(self, monkeypatch):
        from singularity.scheduler import phase_models
        phase_models.save({"extract": ["designated"]})
        assert self._pick(monkeypatch, fusion="from-toml") == "designated"

    def test_fusion_toml_still_works_when_unconfigured(self, monkeypatch):
        # fusion.toml 是 UI 之外的配置路径（CLI / 手改），不能因为新增了阶段配置就断掉
        assert self._pick(monkeypatch, fusion="from-toml") == "from-toml"

    def test_default_when_nothing_configured(self, monkeypatch):
        from singularity.scheduler import execution_judge as ej
        assert self._pick(monkeypatch) == ej._V2_EXTRACT_DEFAULT

    def test_first_entry_wins(self, monkeypatch):
        from singularity.scheduler import phase_models
        phase_models.save({"extract": ["first", "second"]})
        assert self._pick(monkeypatch, fusion="from-toml") == "first"
