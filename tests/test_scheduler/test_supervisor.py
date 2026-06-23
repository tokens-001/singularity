"""supervisor.py 单元测试 — _check_completeness / _check_constraints / _check_laziness。

ponytail: 只测三个纯规则检查函数。supervise() 编排函数已有集成覆盖。
"""

import pytest
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
# _check_completeness
# ═══════════════════════════════════════════════════════════════

class TestCheckCompleteness:
    def test_no_checklist_passes(self):
        """无 checklist → 跳过，通过。"""
        from singularity.scheduler.supervisor import _check_completeness
        r = _check_completeness([], "输出内容", ["app.py"])
        assert r.passed
        assert "跳过" in r.reason

    def test_no_changed_files_fails(self):
        """有 checklist 但无改动文件 → 硬证据失败。"""
        from singularity.scheduler.supervisor import _check_completeness
        r = _check_completeness(["实现登录", "添加测试"], "输出内容", [])
        assert not r.passed
        assert r.evidence.get("hard")

    def test_all_items_covered(self):
        """checklist 全部覆盖 → 通过。"""
        from singularity.scheduler.supervisor import _check_completeness
        r = _check_completeness(
            ["登录功能", "测试用例"],
            "实现了登录功能和测试用例",
            ["app.py", "test_app.py"],
        )
        assert r.passed

    def test_partial_coverage_fails(self):
        """部分 checklist 未覆盖 → 软证据失败。"""
        from singularity.scheduler.supervisor import _check_completeness
        r = _check_completeness(
            ["登录功能", "注册功能", "忘记密码"],
            "实现了登录功能",  # 只覆盖了登录
            ["app.py"],
        )
        assert not r.passed
        assert not r.evidence.get("hard")  # 软证据
        assert "注册功能" in r.reason or "1/3" in r.reason or "2/3" in r.reason

    def test_empty_output_covers_nothing(self):
        """输出为空 → checklist 全部未覆盖。"""
        from singularity.scheduler.supervisor import _check_completeness
        r = _check_completeness(["实现登录"], "", ["app.py"])
        assert not r.passed


# ═══════════════════════════════════════════════════════════════
# _check_constraints
# ═══════════════════════════════════════════════════════════════

class TestCheckConstraints:
    def test_no_constraints_passes(self):
        """无约束 → 跳过。"""
        from singularity.scheduler.supervisor import _check_constraints
        r = _check_constraints([], ["app.py"], Path("/tmp"))
        assert r.passed

    def test_no_violation(self):
        """未触发约束 → 通过。"""
        from singularity.scheduler.supervisor import _check_constraints
        r = _check_constraints(
            ["不改数据库层"],
            ["src/app.py", "src/utils.py"],
            Path("/tmp"),
        )
        assert r.passed

    def test_violation_with_frozen_keyword(self):
        """约束含'冻结' + 文件被改 → 硬证据失败。"""
        from singularity.scheduler.supervisor import _check_constraints
        r = _check_constraints(
            ["冻结 core.py"],
            ["src/core.py"],
            Path("/tmp"),
        )
        assert not r.passed
        assert r.evidence.get("hard")

    def test_violation_with_forbidden_keyword(self):
        """约束含'禁止' → 失败。"""
        from singularity.scheduler.supervisor import _check_constraints
        r = _check_constraints(
            ["禁止修改 config.py"],
            ["config.py"],
            Path("/tmp"),
        )
        assert not r.passed

    def test_same_filename_different_dir_no_violation(self):
        """同文件名但约束不命中 → 通过。"""
        from singularity.scheduler.supervisor import _check_constraints
        r = _check_constraints(
            ["不改 main.py"],
            ["src/sub/main.py"],
            Path("/tmp"),
        )
        # "main.py" 在 "不改 main.py" 中命中
        assert not r.passed

    def test_no_violation_without_keywords(self):
        """约束不含禁改关键词 → 不触发（即使文件匹配）。"""
        from singularity.scheduler.supervisor import _check_constraints
        r = _check_constraints(
            ["检查 app.py 性能"],  # 不含"不改/禁止/冻结/不可改"
            ["app.py"],
            Path("/tmp"),
        )
        assert r.passed


# ═══════════════════════════════════════════════════════════════
# _check_laziness
# ═══════════════════════════════════════════════════════════════

class TestCheckLaziness:
    def test_no_signals_passes(self):
        """无偷懒信号 → 通过。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("完整的代码实现", ["app.py", "test_app.py"], ["实现功能"])
        assert r.passed

    def test_todo_comment_fails(self):
        """输出含 TODO → 失败。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("// TODO: 这里需要完善", ["app.py"], ["实现功能"])
        assert not r.passed
        assert r.evidence.get("hard")

    def test_omit_comment_fails(self):
        """输出含 # 此处省略 → 失败。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("# 此处省略 200 行实现", ["app.py"], ["实现功能"])
        assert not r.passed

    def test_vague_phrase_fails(self):
        """模糊措辞 → 失败。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("这个方案应该能跑", ["app.py"], ["实现功能"])
        assert not r.passed

    def test_theoretically_no_problem(self):
        """"理论上没问题"→ 失败。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("理论上没问题", ["app.py"], ["实现功能"])
        assert not r.passed

    def test_no_test_files_with_checklist(self):
        """有 checklist 但无测试文件改动 → 失败。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("代码实现完成", ["app.py"], ["实现功能", "添加测试"])
        assert not r.passed

    def test_with_test_files_passes(self):
        """有测试文件改动 → 通过偷懒检测。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("代码实现完成", ["app.py", "test_app.py"], ["实现功能"])
        assert r.passed

    def test_few_files_vs_checklist(self):
        """改动文件远少于 checklist 预期 → 失败。"""
        from singularity.scheduler.supervisor import _check_laziness
        # checklist 有 10 项，只有 1 个文件 → max(1, 10//3)=3，1<3
        r = _check_laziness("代码", ["app.py"],
                           ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"])
        assert not r.passed

    def test_no_checklist_no_test_check(self):
        """无 checklist → 不检查测试文件。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("代码实现", ["app.py"], [])
        assert r.passed
