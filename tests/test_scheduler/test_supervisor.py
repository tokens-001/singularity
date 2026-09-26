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

    ⚠️ **2026-09-26 反了一处**（Qoder 第二轮 #6）：原来这里还断言 `r.passed`，
    而那个断言**恰好钉住了本条的谎** —— 跳过 lint 只是半个症状，真正的病是
    整格零次检查（`py_compile` 也跑 0 次）却报「质量门禁通过 (N 文件)」+ `hard: True`。
    「不该判**失败**」这个顾虑仍然成立，处置改成了本仓对"没做"的一贯做法：
    `passed=False` + **`hard=False`** ⇒ 汇总成 `escalate` **交人审**（既不放行也不判失败）。
    底下新增的那条 `test_零检查时走escalate而不是fail` 钉的就是"别变成假红"。
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
        assert [c for c in calls if c and "py_compile" in c] == [], \
            f"文件不在 root 下时也不该去 compile: {calls}"
        assert not r.passed, "一个文件都没查过，却报'通过' —— 零次检查换来一个硬通过"
        assert "未执行" in r.reason, r.reason
        assert r.evidence.get("hard") is False, \
            "零检查还挂着 hard=True ⇒ 汇总会判成硬失败（假红），处置应该是交人审"

    def test_零检查时走escalate而不是fail(self, tmp_path):
        """**接线判据**：`hard=False` 让汇总落到 `escalate`（人审），不是 `fail`。

        变异：把那条提前返回里的 `"hard": False` 改成 `True` ⇒ 本条红
        （verdict 从 escalate 变 fail = 造出一个和任务无关的假红）。
        """
        from singularity.scheduler import supervisor as sv
        v = sv.supervise(
            task_description="改一个模块", changed_files=["pkg/other.py"],
            constraints=[], checklist=[], agent_output="改完了",
            repo_root=str(tmp_path), tests_result={"passed": True},
        )
        assert v.verdict == "escalate", \
            f"零检查该交人审（既不放行也不判失败），实际 {v.verdict}：{v.issues}"
        assert any("未执行" in i for i in v.issues), \
            f"没进 issues ⇒ 人审页上看不见：{v.issues}"

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


# ═══════════════════════════════════════════════════════════════
# 「只跑不改」的任务不该被"零文件改动"判死（2026-09-15 真机）
# ═══════════════════════════════════════════════════════════════
# 现场：planner 拆出一个「独立验收：只跑不改」的任务，它**活干对了**
# （真跑 pytest 8 passed、逐条核对 PRD、给了 evidence），
# 却被 `QA:fail: [completeness] 无文件改动; [laziness] 检测到 2 个偷懒信号` 判死，
# **412 秒就死 —— 与 900s 超时无关**。
#
# ⚠️ 判据**刻意只认固定的协议标记 `[只读]`**（由架构 schema 约定写进任务标题），
# **不做自然语言推断** —— "只跑不改/不修改/纯核对…"是开集，永远有下一个说法。

READONLY_DESC = "[只读] 独立验收：只跑不改，逐条核对 PRD"
NORMAL_DESC = "实现 fizzbuzz.py 纯函数与 CLI 入口"


class TestReadonlyTaskIsNotPunishedForNoChanges:
    def test_只读任务零改动不算完整性失败(self):
        from singularity.scheduler.supervisor import _check_completeness
        r = _check_completeness(["验收通过"], "8 passed", [], READONLY_DESC)
        assert r.passed, f"只读任务被判死了：{r.reason}"
        assert r.evidence.get("readonly") is True

    def test_只读任务零改动不算偷懒(self):
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("pytest 8 passed，逐条核对完毕", [], ["跑测试验证"], READONLY_DESC)
        assert r.passed, f"只读任务被判偷懒：{r.reason}"

    def test_没有声明的任务照旧判死_对照组(self):
        """⚠️ 别顺手把那条硬规则整个改掉了 —— 它的原意（兄弟抢活、自己空手）是真的。"""
        from singularity.scheduler.supervisor import _check_completeness
        r = _check_completeness(["验收通过"], "…", [], NORMAL_DESC)
        assert not r.passed, "没声明只读的任务零改动必须仍然是失败"
        assert r.evidence.get("hard") is True, "而且要是**硬**失败（不能被降级成 escalate）"

    def test_只读任务也可能糊弄_硬信号照常生效(self):
        """声明了 [只读] **不等于免检**：TODO / 模糊措辞照常判死。"""
        from singularity.scheduler.supervisor import _check_laziness
        r = _check_laziness("这块应该能跑，先这样", [], [], READONLY_DESC)
        assert not r.passed, "只读任务说了模糊措辞，居然放行了"
        assert r.evidence.get("hard") is True, "这条该是硬信号"

    def test_默认参数下行为不变(self):
        """不传 task_description 时 = 老行为（老调用点不该被这次改动波及）。"""
        from singularity.scheduler.supervisor import _check_completeness, _check_laziness
        assert not _check_completeness(["x"], "y", []).passed
        assert not _check_laziness("y", [], ["测试" * 5]).passed

    def test_架构schema里必须写着这条约定(self):
        """⚠️ **协议得上下两头都有**：下游认 `[只读]`，上游就得告诉架构师要写它。

        少了上游这一句，planner 永远不知道要打这个标记 ⇒ 豁免代码一次都不会生效
        （= 今天这条修复白做）。同今天 QA 那条：**契约两边要对得上**。
        """
        import inspect
        from singularity.scheduler import workflow as W
        ctx = W._ARCHITECT_CONTEXT
        assert "[只读]" in ctx, "架构 schema 没告诉架构师要打 [只读] 标记 —— 豁免永远走不到"
        assert "title" in ctx, "得说清标记打在哪（标题）—— 标题才会拼进 description"
