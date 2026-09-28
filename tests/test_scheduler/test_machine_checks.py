"""机械检查：解析 / 白名单 / 真跑。

**安全面是这个模块存在的主要理由，所以负面用例比正面用例重要。**
最要紧的一条：`python3 -c` 等于任意代码执行 —— 模型生成的架构 JSON 是
prompt injection 面，这条必须堵死，而且必须**在参数层判**（只判程序名挡不住）。
"""
import re

import pytest

from singularity.scheduler import _machine_checks as mc


class TestParse:
    def test_valid(self):
        assert mc.parse_check({"argv": ["pytest", "-q"], "expect_exit": 0}) == {
            "argv": ["pytest", "-q"], "expect_exit": 0}

    def test_expect_exit_defaults_to_zero(self):
        assert mc.parse_check({"argv": ["pytest"]})["expect_exit"] == 0

    def test_prose_is_not_runnable(self):
        """散文不是"格式错" —— 它表示"这条机器验不了"，是诚实答案。"""
        assert mc.parse_check("界面要好看，机器验不了") is None

    @pytest.mark.parametrize("bad", [
        {}, {"argv": "pytest"}, {"argv": []}, {"argv": [1, 2]},
        {"argv": ["pytest"], "expect_exit": "abc"},
        None, 123, ["pytest"],
    ])
    def test_malformed_is_none(self, bad):
        assert mc.parse_check(bad) is None


class TestWhitelist:
    def test_allowed(self):
        assert mc.validate_check({"argv": ["pytest", "-q"], "expect_exit": 0})[0]
        assert mc.validate_check({"argv": ["git", "diff"], "expect_exit": 0})[0]

    def test_unknown_program_rejected(self):
        ok, why = mc.validate_check({"argv": ["rm", "-rf", "/"], "expect_exit": 0})
        assert not ok and "白名单" in why

    @pytest.mark.parametrize("argv", [
        ["python3", "-c", "import os; os.system('rm -rf /')"],
        ["python", "-c", "print(1)"],
        ["python3"],
        ["python3", "-m", "pip", "install", "evil"],
        ["python3", "-m", "pytestX"],
    ])
    def test_interpreter_escape_rejected(self, argv):
        """整个文件最要紧的用例：解释器只能 `-m pytest`。"""
        ok, why = mc.validate_check({"argv": argv, "expect_exit": 0})
        assert not ok, f"必须拒绝: {argv}"

    def test_python_m_pytest_allowed(self):
        assert mc.validate_check(
            {"argv": ["python3", "-m", "pytest", "-q"], "expect_exit": 0})[0]

    # ── 构建/测试驱动（2026-09-27 加）──────────────────────────────────
    #
    # 来历：白名单原来只有 Python / Node 生态，而提示词里 `tech_stack.language`
    # **毫无限制** ⇒ 模型选了 Go 就一个能用的命令都没有，被迫把 16 条判据全写成
    # `python3 -m pytest` ⇒ `round-20260926` 架构自相矛盾、整轮全灭。

    @pytest.mark.parametrize("argv", [
        ["go", "test", "./..."],
        ["go", "build", "./..."],
        ["go", "vet", "./..."],
        ["cargo", "test"],
        ["cargo", "check"],
        ["rustc", "src/main.rs"],
    ])
    def test_构建驱动放行(self, argv):
        assert mc.validate_check({"argv": argv, "expect_exit": 0})[0], argv

    @pytest.mark.parametrize("argv", [
        ["go", "install", "./..."],                  # 拉外部代码
        ["go", "get", "github.com/x/y"],             # 拉外部代码
        ["go", "env", "-w", "GOFLAGS=-mod=mod"],     # 改本机配置
        ["go"],                                      # 没子命令
        ["cargo", "install", "cargo-edit"],
        ["cargo", "publish"],
    ])
    def test_构建驱动_危险子命令必须拒绝(self, argv):
        """🔴 **负面用例比正面用例重要** —— 加这几个驱动是为了**能跑测试**，
        不是为了**能拉外部代码 / 改本机配置**。跟 `-m pytest` 那条同一个思路：
        **在参数层判，不是只判程序名**。

        判据：把 `_machine_checks` 里 `_TOOL_OK_SUBCOMMAND` 那段判断删掉 ⇒ 这条红。
        """
        ok, why = mc.validate_check({"argv": argv, "expect_exit": 0})
        assert not ok, f"必须拒绝: {argv}"

    # ── node 也吃代码（2026-09-28）────────────────────────────────────
    #
    # 🔴 规则 2 原来只落在 `python3` 上，而 `node` 也在白名单里、**没有任何参数层检查**
    # ⇒ `node -e '<任意代码>'` 直接过闸，而 `python3 -c` 是拒的。
    # 严重度**低**（`npm run` 本来就在跑模型写的脚本，不增加新能力），
    # 但那是「**规则写了、只对一个解释器执行**」—— 单边落地正是本仓栽过的形状。

    @pytest.mark.parametrize("argv", [
        ["node", "-e", "require('fs').rmSync('/', {recursive:True})"],
        ["node", "--eval", "x"],
        ["node", "-p", "process.env"],
        ["node", "--print", "1"],
    ])
    def test_node_内联代码必须拒绝(self, argv):
        """判据：把 `validate_check` 里 `if prog == "node":` 那一段删掉 ⇒ 这条红。"""
        ok, why = mc.validate_check({"argv": argv, "expect_exit": 0})
        assert not ok, f"必须拒绝: {argv}"

    @pytest.mark.parametrize("argv", [
        ["node", "--test"],
        ["node", "tests/x.test.js"],
        ["node", "--test", "tests/"],
        # 🔴 **反例里的反例**：`-p` 在**脚本名之后**是脚本自己的参数，不是 node 的选项
        # ⇒ 全参数扫会误伤一整类正常的 `node <file>` 调用。解析必须停在第一个非选项参数。
        ["node", "script.js", "-p", "8080"],
        ["node", "-r", "./x.js", "main.js"],
    ])
    def test_node_跑文件放行(self, argv):
        assert mc.validate_check({"argv": argv, "expect_exit": 0})[0], argv

    def test_白名单文本从代码现算(self):
        """`argv0_whitelist_text()` 是**唯一**的名单描述 —— 加一个程序，
        文本里就得出现，**不需要改任何提示词**。"""
        t = mc.argv0_whitelist_text()
        for prog in mc.ALLOWED_ARGV0:
            assert prog in t, f"{prog} 在白名单里，但描述文本里没有"
        for prog, subs in mc._TOOL_OK_SUBCOMMAND.items():
            for sub in subs:
                assert sub in t, f"{prog} 的子命令 {sub} 没进描述"

    def test_白名单只有一处定义_全部提示词都是现算的(self):
        """🔴 **两份拷贝必然漂** —— 这是同一个洞的第二次（`防御模式.md` §61 记的）。

        手写的那份当时散在**三处**（架构师 / 定稿提示词 / 定稿 schema），
        我 09-27 只改了一处 ⇒ 又漂了。而漂的方向是**最坏的那一个**：
        代码加了 `go`、提示词没加 ⇒ **模型根本不知道能用它** ⇒ 只能写 pytest ⇒
        「说 Go 但判据是 pytest」的自相矛盾架构 ⇒ `round-20260926` 整轮全灭。

        §61 原话：「**同一份契约只能有一个定义点。两份拷贝必然漂**，
        而且漂的是其中一份 —— 出问题时你盯着 prompt 找原因，根因在另一个文件里。」

        判据：把任意一份里烤进去的文本换回手写名单 ⇒ 这条红。
        """
        from singularity.scheduler import execution_judge as ej
        from singularity.scheduler.workflow import _ARCHITECT_CONTEXT
        want = mc.argv0_whitelist_text()
        for name, text in (("_ARCHITECT_CONTEXT", _ARCHITECT_CONTEXT),
                           ("_V2_FINALIZE", ej._V2_FINALIZE)):
            assert want in text, f"{name} 里的白名单不是现算的那份 ⇒ 它已经漂了"
            assert "__ARGV0_WHITELIST__" not in text, f"{name} 的占位符没被替换"

    def test_架构契约不许写死语言的示范(self):
        """🔴 **示范写死 Python 是这一轮的直接病因** —— 模型是照着示范抄的。

        三处样板都要查（架构师 / 定稿提示词 / 定稿 schema）。注意只能查
        **argv 例子**里的语言：散文里说「别写 `python3 -m pytest`」是**对的**，不该误伤。

        判据：把任意一处改回 `"argv": ["python3","-m","pytest", ...]` ⇒ 这条红。
        """
        from singularity.scheduler import execution_judge as ej
        from singularity.scheduler.workflow import _ARCHITECT_CONTEXT
        bad = re.compile(r'"argv"\s*:\s*\[\s*"(python3|python|pytest)"')
        for name, text in (("_ARCHITECT_CONTEXT", _ARCHITECT_CONTEXT),
                           ("_V2_FINALIZE", ej._V2_FINALIZE),
                           ("_ARCH_SCHEMA", ej._ARCH_SCHEMA)):
            m = bad.search(text)
            assert not m, f"{name} 里又有写死语言的 argv 示范: {m.group(0)}"

    def test_string_check_is_not_runnable(self):
        ok, why = mc.validate_check("跑一下测试看看")
        assert not ok and "argv" in why


class TestCoverage:
    def test_counts_only_runnable(self):
        cs = [{"rule": "r1", "check": {"argv": ["pytest"], "expect_exit": 0}},
              {"rule": "r2", "check": "界面要好看"},
              {"rule": "r3", "check": {"argv": ["rm"], "expect_exit": 0}}]  # 白名单外
        assert mc.coverage(cs) == (1, 3), "白名单外的也算不出来跑"

    def test_empty(self):
        assert mc.coverage([]) == (0, 0)
        assert mc.coverage(None) == (0, 0)


class TestRequirementCoverage:
    """**需求侧**覆盖率 —— 分母是需求（用户侧），不是架构师自己列的约束。

    拿约束当分母的话，他少列一条分母就跟着缩、比例纹丝不动（"自洽率"）；
    拿需求当分母，**漏掉的需求才会以低分暴露**。
    """

    REQS = ["统计行数/词数/字符数", "位置参数收文件路径", "--json 输出", "单文件 + pytest"]

    def test_index_and_text_both_work(self):
        cs = [
            {"rule": "a", "check": "散文", "covers": [0]},              # 索引
            {"rule": "b", "check": "散文", "covers": ["--json 输出"]},   # 原文
        ]
        r = mc.requirement_coverage(cs, self.REQS)
        assert r["total"] == 4 and r["covered"] == 2
        assert r["uncovered"] == [1, 3]

    def test_hard_covered_only_counts_runnable_checks(self):
        """只有**可机器跑**的约束覆盖到的，才算"机器在盯"。"""
        cs = [
            {"rule": "a", "check": "散文", "covers": [0]},                        # 覆盖但不可跑
            {"rule": "b", "check": {"argv": ["pytest"], "expect_exit": 0}, "covers": [1]},
        ]
        r = mc.requirement_coverage(cs, self.REQS)
        assert r["covered"] == 2, "两条都被声明覆盖了"
        assert r["hard_covered"] == 1, "但只有一条是机器在盯"

    def test_uncovered_is_the_high_signal(self):
        """一条约束都没声明的需求 —— 最该看的一栏。"""
        cs = [{"rule": "a", "check": "散文", "covers": [0]}]
        r = mc.requirement_coverage(cs, self.REQS)
        assert r["uncovered"] == [1, 2, 3]

    def test_out_of_range_index_ignored(self):
        cs = [{"rule": "a", "check": "散文", "covers": [0, 99, -1]}]
        r = mc.requirement_coverage(cs, self.REQS)
        assert r["covered"] == 1, "越界索引不许算命中"

    def test_unmatched_keeps_what_was_dropped(self):
        """认不出的 token 不许**悄悄**消失 —— 它是"伪造覆盖率"最省事的那条路。

        真机形状：b 轮 core 6 条，covers 里出现 6/7/8/9 ⇒ 越界的被丢掉、剩下的
        恰好把每栏点亮 ⇒ 报表上「覆盖率 100%」。
        """
        cs = [{"rule": "a", "check": "散文", "covers": [0, 99, "对不上的话"]},
              {"rule": "b", "check": "散文", "covers": [1]}]
        r = mc.requirement_coverage(cs, self.REQS)
        assert r["covered"] == 2 and r["unmatched"] == [99, "对不上的话"], r

    def test_unmatched_empty_when_everything_lands(self):
        """反例：全都对得上时不许乱报，否则它就是个常亮的假红。"""
        r = mc.requirement_coverage([{"rule": "a", "check": "散文", "covers": [0, "0"]}],
                                    self.REQS)
        assert r["unmatched"] == []

    def test_no_requirements_means_every_token_unmatched(self):
        """没清单 ⇒ 每个 covers 都无处可对，这一栏别报成 0（那会被读成"没问题"）。"""
        r = mc.requirement_coverage([{"rule": "a", "check": "散文", "covers": [0]}], [])
        assert r["total"] == 0 and r["unmatched"] == [0]

    def test_bool_is_not_an_index(self):
        """`True` 是 int 的子类 —— 不挡掉的话会被当成索引 1。"""
        cs = [{"rule": "a", "check": "散文", "covers": [True]}]
        assert mc.requirement_coverage(cs, self.REQS)["covered"] == 0

    def test_constraints_without_covers_are_counted(self):
        cs = [{"rule": "a", "check": "散文"}, {"rule": "b", "check": "散文", "covers": [0]}]
        r = mc.requirement_coverage(cs, self.REQS)
        assert r["no_covers"] == 1

    def test_no_requirements_is_zero_not_crash(self):
        r = mc.requirement_coverage([{"rule": "a", "check": "x", "covers": [0]}], [])
        assert r["total"] == 0 and r["covered"] == 0

    def test_substring_match_still_works(self):
        cs = [{"rule": "a", "check": "散文", "covers": ["--json"]}]
        assert mc.requirement_coverage(cs, self.REQS)["covered"] == 1


class TestCleanEnv:
    def test_strips_secrets_and_proxies(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
        monkeypatch.setenv("https_proxy", "http://evil:8080")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
        env = mc._clean_env("/tmp")
        assert "DEEPSEEK_API_KEY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "https_proxy" not in env and "http_proxy" not in env
        assert set(env) == {"PATH", "HOME", "LANG", "LC_ALL"}


class TestRunCheck:
    def test_refuses_what_validation_refuses(self, tmp_path):
        r = mc.run_check({"argv": ["python3", "-c", "print(1)"], "expect_exit": 0}, tmp_path)
        assert r["ran"] is False and r["passed"] is False

    def test_runs_and_reports_exit_code(self, tmp_path):
        r = mc.run_check({"argv": ["pytest", "--version"], "expect_exit": 0}, tmp_path)
        assert r["ran"] is True
        assert r["exit"] is not None

    def test_expect_exit_mismatch_is_not_passed(self, tmp_path):
        r = mc.run_check({"argv": ["pytest", "--version"], "expect_exit": 42}, tmp_path)
        assert r["ran"] is True and r["passed"] is False, "退出码不符就不算过"

    def test_timeout_is_reported_not_hung(self, tmp_path):
        r = mc.run_check({"argv": ["python3", "-m", "pytest", "--collect-only"],
                          "expect_exit": 0}, tmp_path, timeout=0.001)
        assert r["ran"] is True and "超时" in r["reason"]

    def test_missing_root_is_refused(self, tmp_path):
        r = mc.run_check({"argv": ["pytest"], "expect_exit": 0}, tmp_path / "nope")
        assert r["ran"] is False and "根目录" in r["reason"]


class TestArchSchemaCoversContract:
    """防御模式 §61：架构 schema 有**两份拷贝**，字段漂过 —— `covers` 在重流程下全丢，
    于是"8/8 条需求没人验"（覆盖率分子恒 0）。

    光把某一处改对不解决复发，所以这里钉住「两份的 constraints 字段集必须一致」。
    ⚠️ 判"prompt 有没有起作用"要挑**原稿里没有、只有 schema 里有的字段**看 ——
    拿两边都有的字段判，会把"抄来的"当成"要求生效了"（§61 规则 2）。
    """
    CANON = {"type", "rule", "check", "covers"}

    @staticmethod
    def _constraint_fields(prompt: str) -> set[str]:
        """抓「约束对象」那个数组里的示例字段。按方括号配对找数组结尾。

        ⚠️ prompt 里 `"constraints"` 不止一处 —— `data_model` 下也有个同名的字符串数组
        （`"constraints": ["约束"]`）。取**含 `"rule"` 的那个**，那才是约束对象。
        """
        for m in re.finditer(r'"constraints"', prompt):
            j = prompt.index("[", m.end())
            depth, k = 0, j
            while k < len(prompt):
                if prompt[k] == "[":
                    depth += 1
                elif prompt[k] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            fields = set(re.findall(r'"(\w+)"\s*:', prompt[j:k + 1]))
            if "rule" in fields:
                return fields
        raise AssertionError(f"没找到约束对象数组：{prompt[:80]!r}")

    def test_both_copies_declare_the_same_constraint_fields(self):
        from singularity.scheduler.workflow import _ARCHITECT_CONTEXT
        from singularity.scheduler.execution_judge import _ARCH_SCHEMA

        seat = self._constraint_fields(_ARCHITECT_CONTEXT)
        final = self._constraint_fields(_ARCH_SCHEMA)
        assert seat == final, (
            f"两份架构 schema 的 constraints 字段漂了 —— "
            f"席位 prompt={sorted(seat)}，委员会定稿={sorted(final)}")
        assert self.CANON <= seat, f"缺字段: {sorted(self.CANON - seat)}"

    def test_seat_prompt_documents_covers_semantics(self):
        """字段出现在 schema 里 ≠ 模型知道它什么意思。席位 prompt 得有解释，否则照抄不出。"""
        from singularity.scheduler.workflow import _ARCHITECT_CONTEXT
        assert "covers" in _ARCHITECT_CONTEXT
        assert "0 起算的索引" in _ARCHITECT_CONTEXT


# ═══════════════════════════════════════════════════════════════
# 「产物没到」≠「测试没过」（2026-09-20）
# ═══════════════════════════════════════════════════════════════
# 真机 round b/c 里那些 `file or directory not found` 卡在一个歧义上：
# `run_check` 在"跑了但没过"时 `reason` 是空的，调用方那句汇总只读 `passed`
# ⇒ GATE3 上写的是「机械检查 8/10 条通过」，**分不出**那 2 条是
# 「测试真的挂了」还是「测试文件根本没被交付」。
# 这两件事的下一步动作完全不同：前者修代码，后者去查**哪个任务没交付**。

class TestMissingInputs:
    """`missing_inputs` —— 纯函数，从输出里抠不存在的路径。"""

    def test_抠得出_pytest那句(self):
        from singularity.scheduler._machine_checks import missing_inputs
        err = "ERROR: file or directory not found: tests/test_filters.py\n"
        assert missing_inputs("", err) == ["tests/test_filters.py"]

    def test_去重且保序(self):
        from singularity.scheduler._machine_checks import missing_inputs
        err = ("ERROR: file or directory not found: b.py\n"
               "ERROR: file or directory not found: a.py\n"
               "ERROR: file or directory not found: b.py\n")
        assert missing_inputs("", err) == ["b.py", "a.py"]

    def test_断言失败抠不出东西(self):
        """**对照**：真的是测试没过时，这里必须是空的 ——
        不然"产物没到"会变成每一条失败都挂的常亮标签（本仓管这叫假红）。"""
        from singularity.scheduler._machine_checks import missing_inputs
        err = ("FAILED tests/test_x.py::test_a - AssertionError: assert 1 == 2\n"
               "1 failed, 2 passed in 0.03s\n")
        assert missing_inputs("", err) == []

    def test_输出为空也不炸(self):
        from singularity.scheduler._machine_checks import missing_inputs
        assert missing_inputs("", "") == []
        assert missing_inputs(None, None) == []


class TestRunCheckAttribution:
    """接线：真起子进程跑，验 `run_check` 真的把那个字段填上了。

    ⚠️ 用**真的 pytest**（`python -m pytest`）—— 被测的就是"pytest 在文件不存在时
    输出长什么样、我们读不读得懂"，喂替身等于测替身。
    """

    def test_文件不存在时填missing_inputs且仍然passed_False(self, tmp_path):
        import sys
        from singularity.scheduler._machine_checks import run_check
        chk = {"argv": [sys.executable, "-m", "pytest", "tests/test_根本不存在.py"],
               "expect_exit": 0}
        r = run_check(chk, tmp_path)
        assert r["ran"] is True, r
        assert r["passed"] is False, "缺文件绝不能算通过（fail-closed）"
        assert r["missing_inputs"] == ["tests/test_根本不存在.py"], r

    def test_测试真挂了时missing_inputs是空的(self, tmp_path):
        """**对照**：同一个 run_check，换一个"文件在但断言失败"的输入 ⇒ 空。"""
        import sys
        from singularity.scheduler._machine_checks import run_check
        (tmp_path / "test_fails.py").write_text(
            "def test_x():\n    assert 1 == 2\n", encoding="utf-8")
        chk = {"argv": [sys.executable, "-m", "pytest", "test_fails.py"], "expect_exit": 0}
        r = run_check(chk, tmp_path)
        assert r["passed"] is False, r
        assert r["missing_inputs"] == [], f"断言失败被误标成产物没到: {r}"

    def test_通过时也带这个键且为空(self, tmp_path):
        """键**永远在**（下游不用 `if 'missing_inputs' in r`），通过时是空表。"""
        import sys
        from singularity.scheduler._machine_checks import run_check
        (tmp_path / "test_ok.py").write_text(
            "def test_x():\n    assert 1 == 1\n", encoding="utf-8")
        chk = {"argv": [sys.executable, "-m", "pytest", "test_ok.py"], "expect_exit": 0}
        r = run_check(chk, tmp_path)
        assert r["passed"] is True, r
        assert r["missing_inputs"] == []


class TestSplitCheckResults:
    """三分类的**接线**：分错了界面会静默说错话，而不会有人因此收到告警。"""

    @staticmethod
    def _split(rs):
        from singularity.scheduler.workflow import _split_check_results
        return _split_check_results(rs)

    def test_三类各归各的(self):
        unran, timed_out, missing = self._split([
            {"ran": False, "passed": False, "reason": "无法执行: FileNotFoundError"},
            {"ran": True, "passed": False, "reason": "超时 60s"},
            {"ran": True, "passed": False, "missing_inputs": ["tests/test_filters.py"]},
            {"ran": True, "passed": False, "missing_inputs": []},     # 真没过
            {"ran": True, "passed": True, "missing_inputs": []},      # 通过
        ])
        assert len(unran) == 1 and len(timed_out) == 1 and len(missing) == 1

    def test_超时那条不算产物没到(self):
        """超时是**我们的秒数不够**，不是"上游没交付" —— 两者都报的话，
        人会去查错方向（去问哪个任务没交付，而其实是该加 timeout）。"""
        _, timed_out, missing = self._split([{"ran": True, "passed": False, "reason": "超时 60s"}])
        assert len(timed_out) == 1 and missing == []

    def test_空结果是三个空表(self):
        assert self._split([]) == ([], [], [])
