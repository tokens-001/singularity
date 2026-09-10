"""API/IO 健壮性：非法输入该 400 不该 500，TOML 该转义（2026-09-11 审计）。

三处都是"调用方送错东西 → 服务端 500 + 一坨 HTML 报错页"：

1. **JSON body 是数组**：三十多处 handler 都是 `request.get_json(silent=True) or {}`
   然后直接 `.get()`。误发数组 → AttributeError → 500。
   统一在 before_request 挡成 400（`/api/models/import` 是唯一**有意**收数组的端点，走白名单）。
2. **非法 gate 参数**：`Phase(gate)` 对垃圾值抛未捕获的 ValueError → 500。
3. **TOML 不转义**：`_format_kv` 对字符串直接 `f'{k} = "{v}"'`，
   值里带 `"` 或换行会把 TOML 写坏，而读侧 `except: return {}` 把解析失败吞成空配置 ——
   用户的设置静默消失。

**在旧代码上会红、且红得对**（断言失败）：前两条旧代码返回 500，第三条旧代码写出的
TOML 解析不回来。
"""
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import singularity.web.app as webapp                        # noqa: E402
from singularity.scheduler import _io                       # noqa: E402


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


class TestJsonBodyShape:
    def test_array_body_is_400_not_500(self, client):
        r = client.post("/api/tasks", json=["这不是对象"])
        assert r.status_code == 400, f"应 400，实际 {r.status_code}"
        assert r.is_json and "error" in r.get_json()

    def test_scalar_body_is_400(self, client):
        r = client.post("/api/tasks", json="字符串")
        assert r.status_code == 400

    def test_models_import_still_accepts_array(self, client):
        """白名单端点必须不受影响 —— 它是**有意**收数组的。"""
        r = client.post("/api/models/import", json=[{"id": "m1"}])
        assert r.status_code == 200, f"白名单端点被误伤: {r.status_code} {r.data[:120]}"

    def test_missing_body_is_not_rejected_by_the_guard(self, client):
        """没有 body 不该被这条守卫拦（各 handler 自己决定默认值）。"""
        r = client.post("/api/tasks")
        assert r.status_code != 400 or "JSON body" not in str(r.data)


class TestGatePhaseValidation:
    def test_invalid_gate_is_400_not_500(self, client, tmp_path, monkeypatch):
        from singularity.scheduler import project as proj_mod
        from singularity.scheduler import _api_projects as api
        p = proj_mod.create(name="gate-test", description="d")
        r, code = api.project_gate_confirm(p.id, "gate9", "approved")
        assert code == 400, f"非法 gate 应 400，实际 {code} {r}"
        assert "gate" in str(r.get("error", "")).lower()

    def test_empty_gate_still_uses_current_phase(self, tmp_path, monkeypatch):
        from singularity.scheduler import project as proj_mod
        from singularity.scheduler import _api_projects as api
        p = proj_mod.create(name="gate-test-2", description="d")
        _, code = api.project_gate_confirm(p.id, "", "approved")
        assert code != 400, "空 gate 是合法用法（用当前阶段），不该被判非法"


class TestTomlEscaping:
    CASES = ['he said "hi"', "line1\nline2", "back\\slash", "tab\there",
             '混合 "quotes" 与 \\ 和 \n 换行']

    @pytest.mark.parametrize("value", CASES)
    def test_round_trips_through_toml(self, value):
        line = _io._format_kv("k", value)
        parsed = tomllib.loads(line + "\n")
        assert parsed["k"] == value, f"写出去再读回来值变了: {line!r}"

    def test_old_style_would_break_toml(self):
        """对照：不转义的写法写出的 TOML 解析不回来（这就是原 bug）。"""
        value = 'he said "hi"'
        naive = f'k = "{value}"'
        with pytest.raises(Exception):
            tomllib.loads(naive + "\n")
