"""机械检查：解析 / 白名单 / 真跑。

**安全面是这个模块存在的主要理由，所以负面用例比正面用例重要。**
最要紧的一条：`python3 -c` 等于任意代码执行 —— 模型生成的架构 JSON 是
prompt injection 面，这条必须堵死，而且必须**在参数层判**（只判程序名挡不住）。
"""
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
