"""GET/PUT /api/phase-models —— 校验、落盘形状、以及那两条"成功 + warning"。

warning 用**单数键**：前端 `useRun`（lib/toast.ts）读的就是 `r.warning`，
写成 `warnings` 界面上就永远不显示 —— 那正是这个功能要修的病症。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import singularity.web.app as webapp                        # noqa: E402
from singularity.scheduler import config                    # noqa: E402


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def _raw() -> dict:
    p = config.QIDIAN_DIR / "phase_models.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


class TestGet:
    def test_lists_five_phases(self, client):
        d = client.get("/api/phase-models").get_json()
        assert [p["key"] for p in d["phases"]] == [
            "researching", "planning", "executing", "reviewing", "extract"]
        assert all(p["label"] for p in d["phases"])

    def test_unconfigured_is_empty(self, client):
        assert client.get("/api/phase-models").get_json()["custom"] == {}


class TestPut:
    def test_roundtrip(self, client):
        r = client.put("/api/phase-models", json={"map": {"planning": ["a", "b"]}})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        assert _raw() == {"planning": ["a", "b"]}
        assert client.get("/api/phase-models").get_json()["custom"] == {"planning": ["a", "b"]}

    def test_non_dict_map_is_400(self, client):
        for bad in ([], "x", 3, None):
            r = client.put("/api/phase-models", json={"map": bad})
            assert r.status_code == 400, f"{bad!r} 应 400"

    def test_missing_map_is_400(self, client):
        assert client.put("/api/phase-models", json={}).status_code == 400

    def test_unknown_phase_key_is_ignored(self, client):
        client.put("/api/phase-models", json={"map": {"planning": ["a"], "junk": ["b"]}})
        assert _raw() == {"planning": ["a"]}

    def test_empty_list_restores_default(self, client):
        client.put("/api/phase-models", json={"map": {"planning": ["a"]}})
        client.put("/api/phase-models", json={"map": {"planning": []}})
        assert _raw() == {}          # 清空 = 恢复默认，文件里真的不留键

    def test_dedup_keeps_order(self, client):
        client.put("/api/phase-models", json={"map": {"planning": ["b", "a", "b"]}})
        assert _raw() == {"planning": ["b", "a"]}

    def test_no_warning_in_the_normal_case(self, client):
        d = client.put("/api/phase-models",
                       json={"map": {"planning": ["a", "b"], "extract": ["c"]}}).get_json()
        assert "warning" not in d


class TestWarnings:
    def test_extractor_also_a_committee_member(self, client):
        d = client.put("/api/phase-models",
                       json={"map": {"planning": ["a", "b"], "extract": ["a"]}}).get_json()
        assert d["ok"] is True
        assert "warning" in d
        assert "a" in d["warning"]

    def test_single_seat_disables_the_committee(self, client):
        d = client.put("/api/phase-models", json={"map": {"planning": ["only"]}}).get_json()
        assert d["ok"] is True
        assert "warning" in d
        assert "委员会" in d["warning"]

    def test_one_seat_plus_matching_extractor_prefers_the_swap_warning(self, client):
        d = client.put("/api/phase-models",
                       json={"map": {"planning": ["only"], "extract": ["only"]}}).get_json()
        assert "提取员" in d["warning"]

    # ── 「配了一家 ⇒ 某个能力静默失效」的**第三处**（2026-09-21）────────────
    # `executing` 只配一家 ⇒ `_exec.py` 那句「容灾: 切换下一个 agent」对实现任务是
    # **死代码**（链过滤掉唯一一家后就是空的），而此前**没有任何地方说过这件事**。
    # 真机实测：28 个任务里换过模型的 **0 个** —— 不是"换了都不成"，是"一次都没换过"。

    def test_executing_with_one_model_warns_no_fallback(self, client):
        d = client.put("/api/phase-models",
                       json={"map": {"planning": ["a", "b"], "executing": ["only"]}}).get_json()
        assert d["ok"] is True
        assert "warning" in d, "配一家 = 容灾链是空的，这件事必须出声"
        assert "兜底" in d["warning"] and "executing" in d["warning"]

    def test_executing_with_two_models_is_the_normal_case(self, client):
        """反例：配了两家就该闭嘴 —— 别把它修成常亮红。"""
        d = client.put("/api/phase-models",
                       json={"map": {"planning": ["a", "b"], "executing": ["x", "y"]}}).get_json()
        assert "warning" not in d

    def test_researching_has_the_same_rule(self, client):
        """`researching` 的语义也是「第 1 个 = 主力，其余兜底」—— 同一个洞，同一张表。"""
        d = client.put("/api/phase-models",
                       json={"map": {"planning": ["a", "b"], "researching": ["only"]}}).get_json()
        assert "researching" in d.get("warning", "")

    def test_two_problems_report_both_not_just_the_first(self, client):
        """🔴 2026-09-21 真机：同时踩两处时**只看得到第一处** —— 新加的那条兜底告警
        在我正需要它的配置里（extract 在委员会里 + executing 只配一家）**隐身**。

        `warning` 是单数键（前端 `useRun` 读它），所以修法是**把多条拼起来**，
        不是加第二个键 —— 加键等于前端照样只看一条。
        """
        d = client.put("/api/phase-models",
                       json={"map": {"planning": ["a", "b"], "extract": ["a"],
                                     "executing": ["only"]}}).get_json()
        assert "提取员" in d["warning"], d["warning"]
        # ⚠️ 判据**只许用第二条独有的字串**：第一版我写的是 `"兜底" in warning`，
        # 而**第一条自己的文案里也有"兜底"**（"换成兜底模型"）⇒ 变不变异都绿（假绿）。
        assert "失败时没有兜底可切" in d["warning"], \
            f"第二条被第一条吃掉了：{d['warning']}"

    def test_unconfigured_phase_is_not_a_warning(self, client):
        """**没配 ≠ 配了一家**：没配是"回退全池"（候选最多），配一家才是"没有兜底"。"""
        d = client.put("/api/phase-models", json={"map": {"planning": ["a", "b"]}}).get_json()
        assert "warning" not in d
