"""技能绑定的两条轴：阶段级（兜底）+ 模型级（例外，优先）。

**为什么加阶段轴**（2026-09-11 外派评审）：技能原来只绑 `(level, model)`，于是
  · 换 `phase_models.json` 里的模型 → 技能**静默消失**；
  · fallback 链更糟：主力挂了退到链上第 2 个，技能跟着换人 —— 同一个任务
    这次有工具、下次没有，**能力取决于运行时故障模式**。

**顺序是「阶段级优先、模型级兜底」**（2026-09-11 看真实数据后定的）：本机六个模型
绑的是**完全相同**的 5 个技能 —— 那不是"每个模型会什么"，是在用"按模型的界面"表达
"大家都该会这些"。若模型级优先，每个模型都有自己的绑定 ⇒ **阶段配置一个都不生效**，
得先手工清六条才有用；阶段级优先则能**渐进采用**（只配 executing，其余阶段照旧）。
代价：阶段配了之后模型级插不上话，所以前端要标出被覆盖的条目，避免静默失效。

另外这个文件钉住一个路径隔离 bug：`skill_loader` 曾经把 `_QIDIAN_DIR` 算在
**模块级**（导入时定死），而 `config.QIDIAN_DIR` 是运行时可改的 ——
`tests/conftest.py` 的隔离对它无效，测试会往生产的 `.qidian/agents_custom.json`
写（那正是用户在界面上绑的技能）。
"""
import json

import pytest

from singularity.scheduler import config
from singularity.skills import skill_loader as sl


@pytest.fixture
def custom_file(tmp_path, monkeypatch):
    """隔离后的 agents_custom.json 路径（conftest 已把 QIDIAN_DIR 指向 tmp）。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    return tmp_path / "agents_custom.json"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class TestTwoAxes:
    def test_phase_axis_wins_when_present(self, custom_file):
        """阶段配了就用阶段 —— 否则"按阶段区分技能"这个新能力根本不生效。

        本机六个模型绑的是完全相同的一组技能，所以模型级优先等于每个模型都
        "有自己的意见"，阶段配置一个都落不了地。
        """
        sl.set_agent_skills("any", "modelA", ["model-skill"])
        sl.set_agent_skills("any", "", ["phase-skill"], phase="executing")
        assert sl.get_agent_skills("any", "modelA", "executing") == ["phase-skill"]

    def test_model_axis_used_when_phase_not_configured(self, custom_file):
        """该阶段没配 → 回落到模型级（渐进采用：只配 executing，其余照旧）。"""
        sl.set_agent_skills("any", "modelA", ["model-skill"])
        sl.set_agent_skills("any", "", ["phase-skill"], phase="executing")
        assert sl.get_agent_skills("any", "modelA", "planning") == ["model-skill"]

    def test_phase_covers_models_without_own_binding(self, custom_file):
        """换模型不丢技能 —— 这是加阶段轴要解决的问题。"""
        sl.set_agent_skills("any", "", ["phase-skill"], phase="executing")
        assert sl.get_agent_skills("any", "modelB", "executing") == ["phase-skill"]

    def test_empty_when_neither_axis(self, custom_file):
        assert sl.get_agent_skills("any", "modelB", "executing") == []

    def test_no_phase_arg_is_old_behaviour(self, custom_file):
        """不传 phase（老调用点）时，行为与加阶段轴之前逐字节一致。"""
        sl.set_agent_skills("any", "modelA", ["s1"])
        assert sl.get_agent_skills("any", "modelA") == ["s1"]
        assert sl.get_agent_skills("any", "modelB") == []

    def test_writing_phase_does_not_clobber_model(self, custom_file):
        """两条轴在文件里各存各的 —— 切换优先序是读时的事，不该动数据。

        这点很重要：模型级被覆盖后**条目还在**，改回来（清掉那个阶段）就恢复。
        """
        sl.set_agent_skills("any", "modelA", ["model-skill"])
        sl.set_agent_skills("any", "", ["phase-skill"], phase="planning")
        data = _read(custom_file)["_skills"]["any"]
        assert data["modelA"] == ["model-skill"] and data["planning"] == ["phase-skill"]

    def test_clearing_phase_restores_model_binding(self, custom_file):
        """清掉阶段 → 模型级立刻重新生效（可逆，不需要动模型那条）。"""
        sl.set_agent_skills("any", "modelA", ["model-skill"])
        sl.set_agent_skills("any", "", ["phase-skill"], phase="executing")
        assert sl.get_agent_skills("any", "modelA", "executing") == ["phase-skill"]
        sl.set_agent_skills("any", "", [], phase="executing")
        assert sl.get_agent_skills("any", "modelA", "executing") == ["model-skill"]

    def test_empty_list_deletes_key(self, custom_file):
        """空列表 = 删键 —— 留 `[]` 会让"清空后回落到另一条轴"回不去。"""
        sl.set_agent_skills("any", "modelA", ["x"])
        sl.set_agent_skills("any", "modelA", [])
        assert "modelA" not in _read(custom_file)["_skills"]["any"]
        sl.set_agent_skills("any", "", ["phase-skill"], phase="executing")
        assert sl.get_agent_skills("any", "modelA", "executing") == ["phase-skill"]

    def test_no_key_when_both_empty(self, custom_file):
        sl.set_agent_skills("any", "", [])
        assert _read(custom_file).get("_skills", {}).get("any", {}) == {}

    def test_phases_are_independent(self, custom_file):
        sl.set_agent_skills("any", "", ["a"], phase="planning")
        sl.set_agent_skills("any", "", ["b"], phase="executing")
        assert sl.get_agent_skills("any", "m", "planning") == ["a"]
        assert sl.get_agent_skills("any", "m", "executing") == ["b"]


class TestCacheKeyIncludesPhase:
    def test_invalidate_clears_all_phases_of_a_model(self):
        """缓存键是 (level, model, phase)；按两元组 pop 一个都打不中。

        打不中的后果是静默的：改了绑定、读到旧技能。任务表现忽好忽坏，
        而代码看上去"明明清了缓存"。
        """
        # 必须先 import dispatcher：它末尾 `from _dispatch_skills import *`，
        # 直接先 import 后者会拿到 partially-initialized 的模块（仓库既有设计）。
        import singularity.scheduler.dispatcher as _d          # noqa: F401
        from singularity.scheduler import _dispatch_skills as ds
        ds._SKILL_CACHE[("any", "m", "executing")] = ("t", "p", {})
        ds._SKILL_CACHE[("any", "m", "planning")] = ("t", "p", {})
        ds._SKILL_CACHE[("any", "other", "executing")] = ("t", "p", {})
        ds.invalidate_skill_cache("any", "m")
        assert ("any", "m", "executing") not in ds._SKILL_CACHE
        assert ("any", "m", "planning") not in ds._SKILL_CACHE
        assert ("any", "other", "executing") in ds._SKILL_CACHE, "别误伤别的模型"


class TestPathFollowsRuntimeConfig:
    def test_qidian_dir_is_computed_per_call(self, tmp_path, monkeypatch):
        """路径必须现算：算在模块级的话，conftest 的隔离对它无效。

        后果不是"测试不方便"，是**测试往生产的 agents_custom.json 写**
        —— 那份文件正是用户在界面上绑的技能。
        """
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        assert sl._qidian_dir() == tmp_path
        other = tmp_path / "elsewhere"
        monkeypatch.setattr(config, "QIDIAN_DIR", other)
        assert sl._qidian_dir() == other, "改了 QIDIAN_DIR 却没跟着走 = 又回到导入时常量"

    def test_write_lands_in_isolated_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        sl.set_agent_skills("any", "m", ["x"])
        assert (tmp_path / "agents_custom.json").exists()
