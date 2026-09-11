"""phase_models.py 单元测试 — 读写 + 脏数据防御 + selection 优先级。

这一层决定"某个阶段跑哪个模型"，所以两条纪律要钉死：
① 脏数据不能传播（一个坏条目 = 整个阶段静默用错模型）
② **没配置 = 回退到旧行为**（selection 返回 (None, False)），这是本模块的兼容边界
"""

import json
import types

import pytest

from singularity.scheduler import config, phase_models


def _write_raw(text: str):
    (config.QIDIAN_DIR / "phase_models.json").write_text(text, encoding="utf-8")


def _raw() -> dict:
    return json.loads((config.QIDIAN_DIR / "phase_models.json").read_text(encoding="utf-8"))


class TestLoad:
    def test_missing_file_is_empty(self):
        # 最关键的一条：没配置时全仓行为必须和加这个模块之前一样。
        assert phase_models.load() == {}
        assert phase_models.for_phase("planning") == []

    def test_broken_json_is_empty_not_raise(self):
        _write_raw("{ this is not json")
        assert phase_models.load() == {}

    def test_top_level_not_dict_is_empty(self):
        _write_raw('["planning"]')
        assert phase_models.load() == {}

    def test_unknown_phase_key_dropped(self):
        _write_raw(json.dumps({"planning": ["a"], "nonsense": ["b"]}))
        assert phase_models.load() == {"planning": ["a"]}

    def test_non_list_value_dropped(self):
        _write_raw(json.dumps({"planning": "a", "executing": ["b"]}))
        assert phase_models.load() == {"executing": ["b"]}

    def test_non_string_and_blank_entries_dropped(self):
        _write_raw(json.dumps({"planning": ["a", 3, None, "", "  ", {"m": 1}, "b"]}))
        assert phase_models.load() == {"planning": ["a", "b"]}

    def test_dedup_keeps_order(self):
        # 保序很重要：第 1 个是"主力"，去重不能顺手重排。
        _write_raw(json.dumps({"planning": ["b", "a", "b", "a"]}))
        assert phase_models.load() == {"planning": ["b", "a"]}

    def test_empty_list_leaves_no_key(self):
        _write_raw(json.dumps({"planning": []}))
        assert phase_models.load() == {}


class TestSave:
    def test_roundtrip(self):
        phase_models.save({"planning": ["a", "b"], "extract": ["c"]})
        assert _raw() == {"planning": ["a", "b"], "extract": ["c"]}
        assert phase_models.load() == {"planning": ["a", "b"], "extract": ["c"]}

    def test_empty_list_deletes_key(self):
        # 界面上清空某项 = 恢复默认，必须能从文件里真的删掉。
        phase_models.save({"planning": ["a"], "extract": ["c"]})
        phase_models.save({"planning": [], "extract": ["c"]})
        assert _raw() == {"extract": ["c"]}

    def test_save_filters_like_load(self):
        phase_models.save({"planning": ["a", "a", "", 3], "junk": ["x"]})
        assert _raw() == {"planning": ["a"]}

    def test_save_survives_non_dict(self):
        phase_models.save({"planning": ["a"]})
        phase_models.save(None)                    # 不能抛
        assert _raw() == {}


class TestPurgeModel:
    def test_removes_from_all_phases(self):
        phase_models.save({"planning": ["a", "b"], "extract": ["b"]})
        assert phase_models.purge_model("b") is True
        # extract 被清空 → key 一起消失，不留僵尸空键
        assert _raw() == {"planning": ["a"]}

    def test_no_hit_returns_false_and_leaves_file_alone(self):
        phase_models.save({"planning": ["a"]})
        assert phase_models.purge_model("never-there") is False
        assert _raw() == {"planning": ["a"]}

    def test_missing_file_is_noop(self):
        assert phase_models.purge_model("a") is False


class TestSelection:
    """三态优先级 —— 第 2 条（项目优先且**不限制**）是兼容边界，别改。"""

    def test_nothing_configured_is_the_old_behaviour(self):
        lineup, restrict = phase_models.selection("planning")
        assert lineup is None
        assert restrict is False

    def test_phase_config_restricts(self):
        phase_models.save({"planning": ["a", "b"]})
        assert phase_models.selection("planning") == ({"any": ["a", "b"]}, True)

    def test_project_lineup_wins_and_does_not_restrict(self):
        phase_models.save({"planning": ["a", "b"]})
        proj = types.SimpleNamespace(agent_lineup={"any": ["z"]})
        assert phase_models.selection("planning", proj) == ({"any": ["z"]}, False)

    def test_project_lineup_for_another_level_is_ignored(self):
        # agent_lineup 是 dict[level -> list]，只有当前 level 那格算数。
        proj = types.SimpleNamespace(agent_lineup={"D": ["z"]})
        assert phase_models.selection("planning", proj) == (None, False)

    def test_project_without_lineup_falls_through_to_phase_config(self):
        phase_models.save({"planning": ["a"]})
        proj = types.SimpleNamespace(agent_lineup={})
        assert phase_models.selection("planning", proj) == ({"any": ["a"]}, True)

    def test_project_none_is_safe(self):
        assert phase_models.selection("planning", None) == (None, False)

    def test_level_is_honoured(self):
        phase_models.save({"executing": ["a"]})
        assert phase_models.selection("executing", level="D") == ({"D": ["a"]}, True)
