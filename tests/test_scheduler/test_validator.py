"""Validator tests."""
import os, tempfile, pytest
from singularity.scheduler.validator import (
    validate, run_project_tests, crossover_review, post_execution_hook,
    multi_model_review, _extract_json_obj,
)


class TestValidatorV2:
    def setup_method(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = self.tmpdir.name

    def teardown_method(self):
        self.tmpdir.cleanup()

    def _write(self, relpath, content):
        p = os.path.join(self.root, relpath)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)

    def test_run_tests_pytest_pass(self):
        self._write("test_ok.py", "def test_ok(): assert True")
        r = run_project_tests(cwd=self.root)
        assert r["passed"]
        assert r["runner"] == "pytest"

    def test_run_tests_pytest_fail(self):
        self._write("test_fail.py", "def test_oops(): assert False")
        r = run_project_tests(cwd=self.root)
        assert not r["passed"]
        assert r["failures"] > 0

    def test_run_tests_no_tests(self):
        r = run_project_tests(cwd=self.root)
        assert r["runner"] == "none"
        assert r["passed"]
        # 2026-09-12：原来只断言 runner=="none" —— **所以消息写错也没人管**。
        # 真相是"**没找到测试**"（要么环境坏、要么测试文件不在），
        # 而报的却是"pytest/unittest/npm 均不可用"，排查方向直接带偏。
        # 探路2 的 T4 就是这么被误导的。
        assert "没找到测试" in r["output"]
        assert "启动不了" not in r["output"]

    def test_run_tests_missing_dir_says_so(self):
        """目录不存在也要说清楚 —— 别让 `except Exception: continue` 把它吞成
        "三个 runner 都启动不了"。"""
        r = run_project_tests(cwd=os.path.join(self.root, "并不存在"))
        assert r["runner"] == "none"
        assert "目录不存在" in r["output"]

    def test_crossover_review_needs_base_to_see_committed_work(self, tmp_path, monkeypatch):
        """反证：worktree 里改动被 `commit_wt` 提交之后，**不带基准就看不见**。

        看不见 → 早退返回 `verdict:"pass"` → **审查静默漏过整份改动**。
        这是同一个形状的第三处（validator 09-11 修过、orchestrator 09-12 修过）。
        """
        import subprocess

        def g(*a):
            subprocess.run(["git", *a], cwd=str(repo), check=True,
                           capture_output=True, text=True)

        repo = tmp_path / "r"
        repo.mkdir()
        g("init")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (repo / "a.py").write_text("x = 1\n")
        g("add", "-A")
        g("commit", "-m", "base")
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                              capture_output=True, text=True).stdout.strip()
        (repo / "a.py").write_text("x = 2\n")
        g("add", "-A")
        g("commit", "-m", "agent changes in t1_any")   # ← 模拟 commit_wt

        # 不带基准：看不见改动 → 早退
        r1 = crossover_review("t", "o", ["a.py"], "any", cwd=str(repo))
        assert "empty diff" in r1["summary"]
        assert not r1["issues"], "看不见改动却给了 pass —— 正是这条的坑"

        # 带基准：看得见 → 不会早退（走到调模型那步，桩掉它）
        from singularity.scheduler import dispatcher as _disp
        import types as _t

        class _ER:
            raw_output = '{"issues":[],"verdict":"pass","summary":"no issues"}'
        class _R:
            executor_result = _ER()

        monkeypatch.setattr(_disp, "load_agents", lambda: {"any": [{"model": "m1"}]})
        monkeypatch.setattr(_disp, "pick_agent_fallback_chain",
                                 lambda *a, **k: [{"model": "m1"}])
        monkeypatch.setattr(_disp, "dispatch", lambda *a, **k: _R())

        r2 = crossover_review("t", "o", ["a.py"], "any", cwd=str(repo), base_ref=base)
        assert "empty diff" not in r2["summary"], "带了基准就该看得见，不该早退"

    def test_crossover_review_no_files(self):
        r = crossover_review("test", "output", [], "any", "test")
        assert r["verdict"] == "pass"

    def test_post_execution_hook(self):
        class F:
            raw_output = "test passed" * 10
            changed_files = ["a.py"]
        r = post_execution_hook(F(), None)
        assert r["confidence"] >= 0.5
        assert "changed_files_count" in r["quality_signals"]


class TestPropertyValidator:
    """Validator 不变量。"""

    def test_dangerous_pattern_detection_deterministic(self):
        code = "rm -rf / something"
        r1 = validate(code, gate_required=False, task_type="feature",
                      changed_files=["a.py"], snap=None, turn=1, max_turns=3)
        r2 = validate(code, gate_required=False, task_type="feature",
                      changed_files=["a.py"], snap=None, turn=1, max_turns=3)
        assert r1.action == r2.action
        assert r1.verdict == r2.verdict

    def test_confidence_in_range(self):
        class F:
            raw_output = ""
            changed_files = []
        r = post_execution_hook(F(), None)
        assert 0.0 <= r["confidence"] <= 1.0

    def test_empty_output_low_confidence(self):
        class F:
            raw_output = "x" * 50
            changed_files = []
        r = post_execution_hook(F(), None)
        assert r["confidence"] < 0.5

    def test_many_files_warning(self):
        class F:
            raw_output = "ok " * 50
            changed_files = [f"{i}.py" for i in range(15)]
        r = post_execution_hook(F(), None)
        assert any("too many" in w for w in r.get("warnings", []))

    def test_safe_code_passes(self):
        r = validate("print('hello')", gate_required=False, task_type="feature",
                      changed_files=["a.py"], snap=None, turn=1, max_turns=3)
        assert r.action == "pass"


class TestExtractJsonObj:
    """JSON 提取 — 治「审查缺口被正则解析丢」的回归。"""

    def test_nested_issues_array(self):
        raw = ('{"issues":[{"severity":"critical","line":1,"detail":"漏了删除"},'
               '{"severity":"warning","line":2,"detail":"无动画"}],"verdict":"retry"}')
        d = _extract_json_obj(raw)
        assert d["issues"][0]["severity"] == "critical"
        assert d["issues"][1]["severity"] == "warning"
        assert d["verdict"] == "retry"

    def test_surrounded_by_text(self):
        raw = '好的：\n```json\n{"issues":[],"verdict":"pass"}\n```'
        d = _extract_json_obj(raw)
        assert d["issues"] == []
        assert d["verdict"] == "pass"

    def test_no_json_returns_none(self):
        assert _extract_json_obj("这里没有 json") is None

    def test_invalid_json_returns_none(self):
        assert _extract_json_obj("{not valid json}") is None


class TestMultiModelReview:
    """审查回归：闭包 NameError + 嵌套 issues 解析（mock dispatch，不碰真 API）。"""

    def test_parses_nested_issues_without_nameerror(self, monkeypatch, tmp_path):
        import singularity.scheduler.dispatcher as disp

        model_out = ('{"issues":[{"severity":"critical","line":1,'
                     '"detail":"需求2删除未实现"}],"verdict":"retry","summary":"s"}')

        class _Raw:
            raw_output = model_out

        class _Exec:
            executor_result = _Raw()

        monkeypatch.setattr(disp, "load_agents", lambda: {"any": [{"model": "m1"}]})
        monkeypatch.setattr(disp, "agent_api_available", lambda a: True)
        monkeypatch.setattr(disp, "dispatch", lambda *a, **k: _Exec())

        (tmp_path / "todo.html").write_text("<html><body>hi</body></html>")
        r = multi_model_review(
            filepath="todo.html", models=["m1"], cwd=str(tmp_path),
            diff_only=False, requirements="写一个 todo 应用")

        assert r["models_used"] == ["m1"]
        assert r["issues"][0]["severity"] == "critical"
        assert r["verdicts"][0]["verdict"] == "retry"
