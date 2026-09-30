"""「**一个自己的产出文件都没交**」必须拦住 —— 而且拦在**合并那一跳**。

🔴 来历（2026-09-30，`round-20260929e` 真机）：
T6 的描述里明写「你的产出文件（架构声明，**只许改这些**）: `tests/test_e2e.py`」，
而它**改了 T1 的 `cli.py`、自己的产物全历史都没交**，最后**照样判 `done` 并进仓**
（`0d12efc merge c54a5a6 via queue`）。

根因：v3 里任务判 `done` 的**唯一**闸门是"worktree 里有 commit 且能合上" ——
`merge._mark_merged` 一句 `transition(DONE)`，**不看 `validation.action`、不看 `batch.ok`、
更不问"改的是不是你的文件"**。所以两道看见它的检查（多模型审查的 critical、`task_file_overlap`）
**都不拦**：前者只把 action 设成 retry（这条路上没人读），后者只记 issues。

⚠️ **必须用真 git**：判据是"这个 commit 改的文件 vs 架构声明的产出文件"，
替身喂不出真实的 diff。`_build_merge_request` 自己也是走 `changed_files_between` 算的。
"""
from __future__ import annotations

import subprocess

import pytest

from singularity.scheduler import project as P


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "src").mkdir()
    (r / "src" / "cli.py").write_text("x = 1\n")
    (r / "tests").mkdir()
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


@pytest.fixture
def proj_with_declared(tmp_path, monkeypatch):
    """一个架构里**声明了** T6 产出 `tests/test_e2e.py` 的项目。"""
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qd")
    (tmp_path / "qd").mkdir()
    p = P.create(name="_t_own_deliverable", template="feature", description="x")
    p.architecture = {"tasks": [
        {"id": "T1", "title": "实现 CLI", "estimated_files": ["src/cli.py"]},
        {"id": "T6", "title": "集成测试", "estimated_files": ["tests/test_e2e.py"]},
        # 声明**两个**文件的任务 —— 「只交了一半」这条只有它能测出来（2026-09-30 补）。
        {"id": "T7", "title": "解析模块", "estimated_files": ["src/parser.py", "tests/test_parser.py"]},
        # **目录**声明 —— 按前缀判（架构师确实会这么写）
        {"id": "T8", "title": "夹具", "estimated_files": ["tests/fixtures/"]},
    ]}
    P.save(p)
    return p


class _Task:
    def __init__(self, pid, desc):
        self.id = "t6"
        self.project_id = pid
        self.description = desc
        self.depends_on = []


class _Res:
    raw_output = "done"
    changed_files: list = []


class _Disp:
    executor_result = _Res()
    agent_cfg = {"model": "m"}


def _wt(repo):
    """真 `Worktree`（`_process_planner_or_merge` 那几条辅助函数要的是 `.path`，不是 Path）。"""
    from singularity.scheduler._git_worktree import Worktree
    return Worktree(path=repo, name="wt", baseline_ref="", repo_root=repo)


def _ctx(repo, base):
    from singularity.scheduler._types import RunContext
    # batch_id / snapshot_ref 是**必填位置参数**（`snapshot_method` 也得跟着给 ——
    # 审查层靠它判这个 ref 能不能当基准）。merge_queue 非 None = v3 那条路。
    return RunContext(batch_id="b1", snapshot_ref=base,
                      snapshot_method="git", merge_queue=object())


class TestOwnDeliverable:
    def test_一个声明文件都没交要拦住(self, repo, proj_with_declared):
        """🔴 **接线**：这种情况**不许建 merge_request** —— 建了就会被合并、就会判 DONE。"""
        from singularity.scheduler import _exec
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "src" / "cli.py").write_text("x = 2\n")     # 改了**别人的**文件
        _git(repo, "commit", "-aqm", "改了 T1 的文件")

        task = _Task(proj_with_declared.id, "[T6] 集成测试: 写 tests/test_e2e.py")
        holder = [None]
        sig = _exec._process_planner_or_merge(
            task, _ctx(repo, base), 1, "any", False, _wt(repo), _Disp().executor_result, _Disp(),
            [], holder, repo_root=repo,
        )
        assert sig and sig[0] == "missing_own_deliverable", f"没拦住：{sig}"
        assert holder[0] is None, "还建了合并请求 ⇒ 它会照旧被合并、任务照旧判 done"

    def test_交了声明文件就照常合并(self, repo, proj_with_declared):
        """**反方向对照**：真交了产出文件，照旧建合并请求（别把正常路也堵了）。"""
        from singularity.scheduler import _exec
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "tests" / "test_e2e.py").write_text("def test_ok(): assert True\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "交了 T6 的产出")

        task = _Task(proj_with_declared.id, "[T6] 集成测试: 写 tests/test_e2e.py")
        holder = [None]
        sig = _exec._process_planner_or_merge(
            task, _ctx(repo, base), 1, "any", False, _wt(repo), _Disp().executor_result, _Disp(),
            [], holder, repo_root=repo,
        )
        assert sig is None, f"交了产出却被拦：{sig}"
        assert holder[0] is not None, "交了产出文件，却没建合并请求"

    def test_没声明就不判(self, proj_with_declared):
        """⚠️ **架构没给 `estimated_files` = 没有尺子** ⇒ 一个都不判（宁可放过，不拿假尺子卡人）。"""
        from singularity.scheduler import _exec
        t = _Task(proj_with_declared.id, "[T9] 架构里没有这一条的任务")
        assert _exec._missing_own_deliverable(t, ["随便什么.py"]) == ""

    def test_判据文案要说清是哪两个集合(self, proj_with_declared):
        """判词要能让人（和模型）知道**该写哪个文件**，不是一句"没交产物"。"""
        from singularity.scheduler import _exec
        t = _Task(proj_with_declared.id, "[T6] 集成测试: 写 tests/test_e2e.py")
        msg = _exec._missing_own_deliverable(t, ["src/cli.py"])
        assert "tests/test_e2e.py" in msg and "src/cli.py" in msg, msg
        assert "不会被合并" in msg, "没说清后果 ⇒ 模型不知道这一版算不算数"

    # ── 2026-09-30：判据从「一个都没交」放宽到「缺任何一个」────────────────
    # 为什么：`round-20260930f` 8/8 全判 done、合并进仓、`release/*` tag 都打了，
    # 而 gate3 机械检查 7 条**只过 2 条** —— 另外 5 条点名的测试文件全不存在。
    # 任务**交了 .py、没交测试** ⇒ 从「有交集就放行」那条缝里整批过去了。

    def test_只交了一半也要拦住(self, proj_with_declared):
        """🔴 声明 `src/parser.py` + `tests/test_parser.py`，只交了前一个 ⇒ **要拦**。

        这是旧判据（`if _decl & _have: return ""`）放过去的那一类。
        """
        from singularity.scheduler import _exec
        t = _Task(proj_with_declared.id, "[T7] 解析模块: 写 src/parser.py")
        msg = _exec._missing_own_deliverable(t, ["src/parser.py"])
        assert msg, "交了一半却放行了 —— 旧判据那条缝又开了"
        assert "tests/test_parser.py" in msg, f"没点名缺的是哪个：{msg}"

    def test_交齐了就放行(self, proj_with_declared):
        """**反方向**：两个都交了 ⇒ 必须放行（别把正常路也堵了）。"""
        from singularity.scheduler import _exec
        t = _Task(proj_with_declared.id, "[T7] 解析模块: 写 src/parser.py")
        assert _exec._missing_own_deliverable(
            t, ["src/parser.py", "tests/test_parser.py"]) == ""

    def test_目录声明按前缀判(self, proj_with_declared):
        """`tests/fixtures/` 是**目录**声明 —— 下面有文件就算交过（否则会误伤一整类任务）。"""
        from singularity.scheduler import _exec
        t = _Task(proj_with_declared.id, "[T8] 夹具: 建 tests/fixtures/")
        assert _exec._missing_own_deliverable(
            t, ["tests/fixtures/sample.jsonl", "src/别的.py"]) == ""
        assert _exec._missing_own_deliverable(t, ["src/别的.py"]), "目录下一个文件都没有，却放行了"
