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
