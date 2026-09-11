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
