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
        "base": roles.Role(key="base", name="基础", description="",
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

    def test_name_override(self, role_env):
        tmp, roles = role_env
        _write(tmp, {"base": {"name": "改名了"}})
        roles._apply_overrides()
        assert roles.ROLES["base"].name == "改名了"

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
        # 2026-09-11: "fixing" 随 Phase.FIXING 一并删除 —— 那个状态全仓无人赋值，
        # 映射一个进不去的阶段只会让人以为"阶段→角色"里还有它。
        assert "fixing" not in roles._DEFAULT_PHASE_ROLES

    def test_config_overrides_default(self, tmp_path, monkeypatch):
        roles = self._cfg(tmp_path, monkeypatch)
        (tmp_path / "phases.json").write_text(json.dumps({"executing": "architect"}), encoding="utf-8")
        assert roles.get_phase_role("executing") == "architect"

    def test_enum_accepted(self, tmp_path, monkeypatch):
        roles = self._cfg(tmp_path, monkeypatch)
        from singularity.scheduler.project import Phase
        assert roles.get_phase_role(Phase.EXECUTING) == "implementer"

    def test_research_and_planning_roles(self, tmp_path, monkeypatch):
        """调研/架构的提示词已搬进 roles.toml，映射也应指向它们。"""
        roles = self._cfg(tmp_path, monkeypatch)
        assert roles.get_phase_role("researching") == "surveyor"
        assert roles.get_phase_role("planning") == "architect"

    def test_unknown_phase_is_empty(self, tmp_path, monkeypatch):
        """没有 agent 参与的阶段不映射角色。"""
        roles = self._cfg(tmp_path, monkeypatch)
        assert roles.get_phase_role("delivering") == ""
        assert roles.get_phase_role("gate1") == ""

    def test_broken_file_falls_back_to_default(self, tmp_path, monkeypatch):
        roles = self._cfg(tmp_path, monkeypatch)
        (tmp_path / "phases.json").write_text("{ 坏的", encoding="utf-8")
        assert roles.get_phase_role("executing") == "implementer"


class TestDefinitionLayerRoles:
    """定义层 4 个角色并入 roles.toml 后，取提示词必须能通。

    以前 _definition_role_prompt 一调就 NameError —— _OBSERVER_DEFINITION_ROLES
    定义在 _observer_tools，本模块用 global 引用却没导入。定义层角色提示词从没生效过。
    """

    def test_all_four_roles_resolve(self):
        from singularity.scheduler._observer_definition import _definition_role_prompt
        for k in ("product-manager", "interaction-designer", "ui-designer", "researcher"):
            assert len(_definition_role_prompt(k)) > 200, k

    def test_legacy_key_still_matches(self):
        """历史遗留的 observer-researcher（SKILL.md 里的 name）也要能命中。"""
        from singularity.scheduler._observer_definition import _definition_role_prompt
        assert _definition_role_prompt("observer-researcher") == _definition_role_prompt("researcher")

    def test_unknown_key_is_empty(self):
        from singularity.scheduler._observer_definition import _definition_role_prompt
        assert _definition_role_prompt("完全不存在的角色") == ""
        assert _definition_role_prompt("") == ""


class TestRolePhases:
    """角色的适用阶段是角色自己的属性 —— 用于过滤「阶段 → 角色」下拉。"""

    def test_loaded_from_toml(self):
        from singularity.scheduler.roles import ROLES
        assert ROLES["surveyor"].phases == ["researching"]
        assert ROLES["architect"].phases == ["planning"]
        assert set(ROLES["implementer"].phases) == {"executing"}
        assert ROLES["reviewer"].phases == ["reviewing"]

    def test_auxiliary_review_roles_are_not_phase_candidates(self):
        """QA 验收 / 安全审计是审查阶段里并列的独立调用，不占阶段映射位。

        标成 ["reviewing"] 会出现在审查下拉里 —— 选了它，代码审查就会用 QA 的
        提示词，而 QA 提示词明写"不做代码审查"，自己跟自己打架。
        """
        from singularity.scheduler.roles import ROLES
        assert ROLES["qa_engineer"].phases == []
        assert ROLES["security_auditor"].phases == []
        assert [k for k, r in ROLES.items() if "reviewing" in r.phases] == ["reviewer"]

    def test_definition_layer_roles_have_no_phase(self):
        """定义层角色切换靠对话，不属于研发阶段 —— 不该出现在阶段下拉里。"""
        from singularity.scheduler.roles import ROLES
        for k in ("product-manager", "interaction-designer", "ui-designer", "researcher"):
            assert ROLES[k].phases == [], k

    def test_override_can_change_phases(self, role_env):
        tmp, roles = role_env
        _write(tmp, {"base": {"phases": ["executing"]}})
        roles._apply_overrides()
        assert roles.ROLES["base"].phases == ["executing"]
