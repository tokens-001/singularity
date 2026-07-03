"""Validator tests."""
import os, tempfile, pytest
from singularity.scheduler.validator import validate, run_project_tests, crossover_review, post_execution_hook


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
