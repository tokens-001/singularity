"""test_project.py — ProjectState 状态机 + P3 成本/血缘 单测。

不依赖 git，不污染工作区，纯逻辑测试。
"""

import pytest
from scheduler.project import (
    ProjectState, Phase, create, save, load, list_all, _REJECT_FALLBACK,
    _GATE_NEXT, TEMPLATES,
)


class TestProjectCRUD:
    def test_create_and_save(self):
        p = create(name="test-crud", description="验证CRUD")
        assert p.id
        assert p.phase == Phase.TEMPLATE
        assert p.name == "test-crud"

        # load
        p2 = load(p.id)
        assert p2 is not None
        assert p2.name == "test-crud"

    def test_list_all(self):
        # 先创建一个，确保 list_all 能找到
        create(name="test-list-all")
        projects = list_all()
        assert any(pr.name == "test-list-all" for pr in projects)

    def test_fields_default(self):
        p = ProjectState(id="t1", name="t")
        assert p.token_budget_total == 5.0
        assert p.token_spent == 0.0
        assert p.phase == Phase.TEMPLATE
        assert p.description == ""
        assert p.raw_constraints == []


class TestPhaseMachine:
    def test_advance_to(self):
        p = create(name="test-phase")
        assert p.advance_to(Phase.RESEARCHING)
        assert p.phase == Phase.RESEARCHING

    def test_gate_approve(self):
        p = create(name="test-gate")
        p.phase = Phase.GATE1
        next_p = p.confirm_gate(Phase.GATE1, "approved")
        assert next_p == Phase.PLANNING
        assert p.phase == Phase.PLANNING
        assert p.owner_confirm["gate1"] == "approved"

    def test_gate_reject(self):
        p = create(name="test-reject")
        p.phase = Phase.GATE2
        fallback = p.confirm_gate(Phase.GATE2, "rejected")
        assert fallback == Phase.RESEARCHING  # GATE2 fallback
        assert p.phase == Phase.RESEARCHING

    def test_gate_chain_full_flow(self):
        """完整 Gate 链: TEMPLATE → GATE1 → PLANNING → GATE2 → EXECUTING → GATE3 → REVIEWING → GATE4 → DONE"""
        p = create(name="test-flow")
        assert p.phase == Phase.TEMPLATE

        # GATE1: approve → PLANNING
        p.phase = Phase.GATE1
        p.confirm_gate(Phase.GATE1, "approved")
        assert p.phase == Phase.PLANNING

        # GATE2: approve → EXECUTING
        p.phase = Phase.GATE2
        p.confirm_gate(Phase.GATE2, "approved")
        assert p.phase == Phase.EXECUTING

        # GATE3: approve → REVIEWING
        p.phase = Phase.GATE3
        p.confirm_gate(Phase.GATE3, "approved")
        assert p.phase == Phase.REVIEWING

        # GATE4: approve → DONE
        p.phase = Phase.GATE4
        p.confirm_gate(Phase.GATE4, "approved")
        assert p.phase == Phase.DONE

    def test_architecture_redo(self):
        p = create(name="test-redo")
        p.phase = Phase.EXECUTING
        p.architecture = {"plan": "test"}
        p.constraints_checklist = ["c1"]
        assert p.architecture_redo()
        assert p.phase == Phase.PLANNING
        assert p.architecture is None
        assert p.constraints_checklist == []

    def test_architecture_redo_wrong_phase(self):
        p = create(name="test-no-redo")
        p.phase = Phase.TEMPLATE
        assert not p.architecture_redo()

    def test_is_at_gate(self):
        p = create(name="test-is-gate")
        p.phase = Phase.GATE2
        assert p.is_at_gate()
        p.phase = Phase.EXECUTING
        assert not p.is_at_gate()


class TestCost:
    def test_spend_tokens_opus(self):
        p = create(name="test-cost")
        cost = p.spend_tokens("claude-opus", 100_000)
        assert cost == pytest.approx(1.5)   # $15/M * 0.1M
        assert p.token_spent == pytest.approx(1.5)

    def test_spend_tokens_deepseek(self):
        p = create(name="test-cost-ds")
        p.spend_tokens("deepseek-v4", 500_000)
        assert p.token_spent == pytest.approx(0.25)  # $0.5/M * 0.5M

    def test_spend_tokens_glm(self):
        p = create(name="test-cost-glm")
        p.spend_tokens("glm-5.2", 200_000)
        assert p.token_spent == pytest.approx(0.2)   # $1/M * 0.2M

    def test_unknown_model_defaults_to_glm_rate(self):
        p = create(name="test-cost-unknown")
        p.spend_tokens("unknown-model", 1_000_000)
        assert p.token_spent == 1.0  # default rate $1/M

    def test_over_budget(self):
        p = create(name="test-over", budget=2.0)
        assert not p.over_budget()
        p.spend_tokens("claude-opus", 200_000)  # $3.0 > $2.0
        assert p.over_budget()

    def test_remaining_calculation(self):
        p = create(name="test-rem", budget=10.0)
        p.spend_tokens("claude-opus", 100_000)  # $1.5
        remaining = max(0, p.token_budget_total - p.token_spent)
        assert remaining == pytest.approx(8.5)


class TestLineage:
    def test_add_lineage(self):
        p = create(name="test-lineage")
        p.add_lineage({"event": "test", "msg": "hello"})
        assert len(p.lineage) == 1
        assert p.lineage[0]["event"] == "test"
        assert "ts" in p.lineage[0]

    def test_lineage_save_load_roundtrip(self):
        p = create(name="test-lin-rt")
        p.add_lineage({"event": "e1"})
        p.add_lineage({"event": "e2"})
        save(p)
        p2 = load(p.id)
        assert p2 is not None
        assert len(p2.lineage) == 2

    def test_lineage_cap_1000(self):
        p = create(name="test-cap")
        for i in range(1100):
            p.add_lineage({"event": f"e{i}"})
        assert len(p.lineage) == 1000
        assert p.lineage[-1]["event"] == "e1099"


class TestTemplates:
    def test_templates_exist(self):
        assert "product_dev" in TEMPLATES
        assert "bug_fix" in TEMPLATES
        assert "refactor" in TEMPLATES

    def test_template_fields(self):
        t = TEMPLATES["product_dev"]
        assert len(t["fields"]) >= 3
        assert "项目名称" in t["fields"]

    def test_create_with_template(self):
        p = create(name="test-tpl", template="bug_fix", budget=3.0)
        assert p.template == "bug_fix"
        assert p.token_budget_total == 3.0
