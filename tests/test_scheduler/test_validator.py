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
