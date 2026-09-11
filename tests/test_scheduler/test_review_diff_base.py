"""审查取 diff 的基准 —— P0-1 回归测试（2026-09-11 审计）。

**这个 bug**：worktree 路径下，改动在 `validate()` **之前**就被
`_exec._process_planner_or_merge` 里的 `commit_wt` 提交了，所以审查用的裸
`git diff` / `git diff HEAD` **恒为空** —— 5 道门禁全都看不到改动，静默放行。

修法：用**执行前快照**的 ref 当基准（`snapshot.take` 在 worktree 干净时兜底记
HEAD，所以 ref 恒是合法 git ref）。

**关于"在旧代码上红"**（本仓库的硬规矩，这里要说清楚）：
旧代码上这些用例**会红，但红的原因是 `TypeError: unexpected keyword argument 'base'`**，
不是断言失败 —— 按 `docs/死代码清单-20260909.md` 第五节立的规矩，这属于"因别的原因变红"，
**不能**当作原始 bug 的回归判据。

所以本文件分两类：
- `test_removed_auth_caught_with_snapshot_base` —— **锁契约**：保证基准参数接上了。
- `test_head_base_sees_nothing` + `test_copy_method_has_no_git_ref` —— **缺陷证据**：
  在**当前代码**上证明"用 HEAD 当基准什么都看不见"，即 P0-1 本身。
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import config as cfg_mod             # noqa: E402
from singularity.scheduler import snapshot as snap_mod          # noqa: E402
from singularity.scheduler import validator as val_mod          # noqa: E402


def _git(repo: Path, *args):
    return subprocess.run(["git", *args], cwd=str(repo),
                          capture_output=True, text=True)


@pytest.fixture
def repo_with_removed_auth(tmp_path):
    """造一个真 git 仓库：基准版本有 require_auth，改后版本删掉了它。"""
    # conftest 把 SNAPSHOT_DIR 重定向进了 tmp 但没建目录；snapshot.take 要往里写 meta
    cfg_mod.ensure_dirs()
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "app.py").write_text("def handler(req):\n    require_auth(req)\n    return 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")

    snap = snap_mod.take("snap-test-1", repo_root=r)

    # 模拟 worktree 里 agent 改完 → commit_wt 提交
    (r / "app.py").write_text("def handler(req):\n    return 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "worktree commit")
    return r, snap


class TestDiffBase:
    def test_git_method_returns_ref(self):
        class S:
            method, ref = "git", "abc123"
        assert val_mod._diff_base(S()) == "abc123"

    def test_copy_method_has_no_git_ref(self):
        class S:
            method, ref = "copy", "/tmp/somedir"
        assert val_mod._diff_base(S()) == ""

    def test_empty_ref_and_none(self):
        class S:
            method, ref = "git", ""
        assert val_mod._diff_base(S()) == ""
        assert val_mod._diff_base(None) == ""

    def test_snapshot_take_always_yields_git_ref(self, tmp_path):
        """干净工作区时 snapshot.take 兜底记 HEAD —— _diff_base 拿得到东西。"""
        cfg_mod.ensure_dirs()
        r = tmp_path / "clean"
        r.mkdir()
        _git(r, "init", "-q")
        _git(r, "config", "user.email", "t@t")
        _git(r, "config", "user.name", "t")
        (r / "a.py").write_text("x = 1\n")
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", "base")
        snap = snap_mod.take("snap-test-clean", repo_root=r)
        assert snap.method == "git"
        assert val_mod._diff_base(snap)


class TestSnapProxyCarriesMethod:
    """P0 回归（2026-09-11 外派评审抓到）：`_SnapProxy` 曾经**没有 method 属性**。

    执行路径上 `_exec.run` 构造的就是它（`snap = _SnapProxy(ctx.snapshot_ref)`），
    一路传给 `validate()` 和 `run_post_exec_checks()`。而 `_diff_base` 的判据是
    `getattr(snap, "method", "") == "git"` —— 对没有该属性的 proxy 恒为 ""，
    于是**基准恒为空**：

      - `validator._hard_diff_rules` 拿不到基准（有披露，但整个检查没跑）；
      - `_review._is_trivial_change` 退回裸 `git diff` → worktree 里改动已被
        `commit_wt` 提交 → 恒 0 行 → **单文件改动恒判 trivial** → 测试 / 多模型审查 /
        QA 验收 / 需求对账 / 安全审计**五道一起短路**，且披露文案说的是"改动被判为小改动"，
        跟真实原因（拿不到基准）不是一回事。

    **这个文件里前面那几条 `class S: method, ref = ...` 的用例全都测不到它** ——
    它们用的是鸭子类型假对象，恰好**有** method。真身漏字段，假对象测不出来。
    """

    def test_proxy_with_git_method_keeps_ref(self):
        from singularity.scheduler._types import _SnapProxy
        assert val_mod._diff_base(_SnapProxy("abc123")) == "abc123"

    def test_proxy_copy_method_yields_no_base(self):
        from singularity.scheduler._types import _SnapProxy
        assert val_mod._diff_base(_SnapProxy("/tmp/d", method="copy")) == ""

    def test_run_context_carries_snapshot_method(self):
        """ctx 必须把 method 从真快照带过来 —— 丢了它就等于回到 P0。"""
        from singularity.scheduler._types import RunContext
        assert RunContext(batch_id="b", snapshot_ref="r").snapshot_method == "git"
        assert RunContext(batch_id="b", snapshot_ref="r",
                          snapshot_method="copy").snapshot_method == "copy"


class TestHardDiffRules:
    def test_removed_auth_caught_with_snapshot_base(self, repo_with_removed_auth):
        """核心回归：用快照 ref 当基准，能抓到被删掉的 require_auth。"""
        r, snap = repo_with_removed_auth
        res = val_mod._hard_diff_rules(["app.py"], cwd=str(r), base=snap.ref)
        assert "no-weaken-security" in [i["rule"] for i in res["issues"]], res

    def test_head_base_sees_nothing(self, repo_with_removed_auth):
        """对照：同样的代码，旧判据(HEAD)什么也报不出 —— 这正是 P0-1。

        这条断言的是**缺陷本身**，所以它在修复前后都通过；它是用来锁住
        "为什么必须换成快照基准" 的证据，不是回归条件。
        """
        r, _ = repo_with_removed_auth
        res = val_mod._hard_diff_rules(["app.py"], cwd=str(r), base="HEAD")
        assert "no-weaken-security" not in [i["rule"] for i in res["issues"]], res

    def test_no_base_skips_and_reports_clean(self, repo_with_removed_auth):
        """拿不到基准时不检查（由 validate() 记 unverified 披露）。"""
        r, _ = repo_with_removed_auth
        res = val_mod._hard_diff_rules(["app.py"], cwd=str(r), base="")
        assert res["issues"] == [] and res["passed"] is True
