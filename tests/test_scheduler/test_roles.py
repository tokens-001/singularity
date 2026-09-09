"""角色覆盖层（roles_custom.json）—— 改 / 增 / 删。

曾经的坑：覆盖只认 persona 和 level，**system_prompt 写进文件却不生效**（改提示词白改），
而且没有新增/删除的路径。roles.toml 是出厂默认，用户改动一律进覆盖层。
"""
import json
import pytest


@pytest.fixture
def role_env(tmp_path, monkeypatch):
    from singularity.scheduler import config, roles
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(roles, "PERSONAS", {})
    monkeypatch.setattr(roles, "ROLES", {
        "base": roles.Role(key="base", name="基础", level="", description="",
                           system_prompt="原始提示词"),
    })
    return tmp_path, roles


def _write(path, data):
    (path / "roles_custom.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestRoleOverrides:

    def test_system_prompt_override_applies(self, role_env):
        """核心回归：改提示词必须生效（以前只认 persona/level）。"""
        tmp, roles = role_env
        _write(tmp, {"base": {"system_prompt": "改过的提示词"}})
        roles._apply_overrides()
        assert roles.ROLES["base"].system_prompt == "改过的提示词"

    def test_new_role_is_created(self, role_env):
        tmp, roles = role_env
        _write(tmp, {"architect": {"name": "架构师", "system_prompt": "你是架构师。"}})
        roles._apply_overrides()
        assert roles.ROLES["architect"].name == "架构师"
        assert roles.ROLES["architect"].system_prompt == "你是架构师。"

    def test_deleted_flag_removes_role(self, role_env):
        tmp, roles = role_env
        _write(tmp, {"base": {"deleted": True}})
        roles._apply_overrides()
        assert "base" not in roles.ROLES

    def test_delete_wins_over_other_fields(self, role_env):
        """墓碑和其他字段同时存在时，删除优先 —— 否则"删了又活过来"。"""
        tmp, roles = role_env
        _write(tmp, {"base": {"deleted": True, "system_prompt": "不该生效"}})
        roles._apply_overrides()
        assert "base" not in roles.ROLES

    def test_capabilities_and_name_override(self, role_env):
        tmp, roles = role_env
        _write(tmp, {"base": {"name": "改名了", "capabilities": ["写代码", "写测试"]}})
        roles._apply_overrides()
        assert roles.ROLES["base"].name == "改名了"
        assert roles.ROLES["base"].capabilities == ["写代码", "写测试"]

    def test_missing_file_is_noop(self, role_env):
        tmp, roles = role_env
        roles._apply_overrides()
        assert roles.ROLES["base"].system_prompt == "原始提示词"

    def test_broken_file_does_not_raise(self, role_env):
        tmp, roles = role_env
        (tmp / "roles_custom.json").write_text("{ 不是 json", encoding="utf-8")
        roles._apply_overrides()                     # 不抛，保留出厂定义
        assert roles.ROLES["base"].system_prompt == "原始提示词"


class TestPhaseRoleMap:
    """阶段 → 角色映射。默认值必须等于改造前的写死行为。"""

    def _cfg(self, tmp_path, monkeypatch):
        from singularity.scheduler import config, roles
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        return roles

    def test_defaults_match_current_behaviour(self, tmp_path, monkeypatch):
        roles = self._cfg(tmp_path, monkeypatch)
        assert roles.get_phase_role("executing") == "implementer"
        assert roles.get_phase_role("fixing") == "implementer"

    def test_config_overrides_default(self, tmp_path, monkeypatch):
        roles = self._cfg(tmp_path, monkeypatch)
        (tmp_path / "phases.json").write_text(json.dumps({"executing": "architect"}), encoding="utf-8")
        assert roles.get_phase_role("executing") == "architect"

    def test_enum_accepted(self, tmp_path, monkeypatch):
        roles = self._cfg(tmp_path, monkeypatch)
        from singularity.scheduler.project import Phase
        assert roles.get_phase_role(Phase.EXECUTING) == "implementer"

    def test_unknown_phase_is_empty(self, tmp_path, monkeypatch):
        """没配的阶段不注入角色 —— 和改造前一致（调研/架构本来就没角色）。"""
        roles = self._cfg(tmp_path, monkeypatch)
        assert roles.get_phase_role("researching") == ""
        assert roles.get_phase_role("planning") == ""

    def test_broken_file_falls_back_to_default(self, tmp_path, monkeypatch):
        roles = self._cfg(tmp_path, monkeypatch)
        (tmp_path / "phases.json").write_text("{ 坏的", encoding="utf-8")
        assert roles.get_phase_role("executing") == "implementer"
