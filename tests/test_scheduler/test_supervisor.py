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

    def test_partial_coverage_recorded_not_failed(self):
        """部分 checklist 未逐字命中 → 只记录, 不判失败 (机械匹配不可靠)。"""
        from singularity.scheduler.supervisor import _check_completeness
        r = _check_completeness(
            ["登录功能", "注册功能", "忘记密码"],
            "实现了登录功能",  # 只覆盖了登录
            ["app.py"],
        )
        assert r.passed                       # 不判失败
        assert not r.evidence.get("hard")
        assert "注册功能" in r.evidence["unverified_items"]

    def test_empty_output_recorded_not_failed(self):
        """输出为空 → 全部未命中, 但仍只记录不判失败。"""
        from singularity.scheduler.supervisor import _check_completeness
        r = _check_completeness(["实现登录"], "", ["app.py"])
        assert r.passed
        assert r.evidence["unverified_items"] == ["实现登录"]


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

    def test_todo_in_filename_is_not_laziness(self):
        """文件名/路径里的 "todo" 不是偷懒标记（2026-09-11 实测误伤）。

        **在旧代码上会红、且红得对**（因断言失败，不是因 TypeError）：
        旧判据是裸子串 `"todo" in agent_output.lower()`，下面每一句都会被判 fail。

        真实事故：一个**完整实现**了 todo.py（原子写、损坏文件容错、内置自测）
        的任务，因为交付物叫 todo.py，被判 `QA:fail: [laziness]` → 未合并 → 产物为零。
        """
        from singularity.scheduler.supervisor import _check_laziness
        for text in ("这是 todo.py 的实现",
                     "<!-- @files: todo.py -->",
                     "# todo.py 的实现",
                     "数据存到 .todo.json 路径",
                     "todo_list 里没有遗漏"):
            assert _check_laziness(text, ["todo.py"], ["实现功能"]).passed, text
        # 对照：真正的注释标记仍然必须拦下
        assert not _check_laziness("# TODO: 实现", ["app.py"], ["实现功能"]).passed
        assert not _check_laziness("x = 1  # TODO", ["app.py"], ["实现功能"]).passed

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

    def test_few_files_signal_is_soft(self):
        """改动文件数 vs checklist 是启发式 → 软信号 (一个文件的精准修复也会命中)。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("代码", ["app.py"],
                           ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"])
        assert not r.passed
        assert not r.evidence.get("hard")

    def test_no_test_file_signal_is_soft(self):
        """无测试文件改动是启发式 → 软信号 (部分任务本就不需要改测试)。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("代码实现完成", ["app.py"], ["实现功能", "添加测试"])
        assert not r.passed
        assert not r.evidence.get("hard")

    def test_no_test_signal_needs_test_in_checklist(self):
        """checklist 没提测试 → 无测试文件改动不算偷懒信号。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("代码实现完成", ["app.py"], ["实现功能"])
        assert r.passed

    def test_mixed_signals_stay_hard(self):
        """硬信号 + 软信号同时命中 → 仍判硬。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("// TODO 待补", ["app.py"], ["a", "b", "c", "d", "e", "f"])
        assert not r.passed
        assert r.evidence.get("hard")


class TestArtifactLintSkip:
    """_check_artifact 的 lint 段：目标文件不在 root 下时必须跳过。

    以前会把**空的**路径列表交给 `ruff check --select=E,F`，而 cwd=root ——
    不带路径的 ruff 是"扫当前目录"，等于拿整个仓库别人的 E/F 违规把这个任务
    判成硬失败（reason=质量门禁失败）。代码注释本意就是"跳过"。
    """

    def test_skips_when_no_changed_file_under_root(self, tmp_path, monkeypatch):
        """必须直接断言"没拿空路径去调 ruff"。

        本机没装 ruff，只断言证据文本的话旧代码会走 FileNotFoundError 分支、
        恰好也含 "skipped" —— 测试在旧代码上照样绿，等于没测。打桩 subprocess.run
        才看得出它到底有没有被调用。
        """
        from types import SimpleNamespace
        from singularity.scheduler import supervisor as sv
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sv.subprocess, "run", fake_run)
        # changed_files 是别处的相对路径 → root 下根本不存在
        r = sv._check_artifact(["pkg/other.py"], tmp_path, tests_result={"passed": True})
        assert [c for c in calls if c and c[0] == "ruff"] == [], \
            f"无目标文件时不该调 ruff（空路径 = 扫整个仓库）: {calls}"
        assert r.passed, f"不该因为没 lint 目标就判失败: {r.reason}"

    def test_still_checks_existing_files(self, tmp_path, monkeypatch):
        """文件真在 root 下时照常走 lint（别把正常路径也跳过了）。"""
        from types import SimpleNamespace
        from singularity.scheduler import supervisor as sv
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sv.subprocess, "run", fake_run)
        (tmp_path / "ok.py").write_text("x = 1\n")
        r = sv._check_artifact(["ok.py"], tmp_path, tests_result={"passed": True})
        ruff_calls = [c for c in calls if c and c[0] == "ruff"]
        assert len(ruff_calls) == 1, f"该调 ruff 却没调: {calls}"
        assert "ok.py" in " ".join(ruff_calls[0]), ruff_calls[0]
        assert r.passed, r.reason
