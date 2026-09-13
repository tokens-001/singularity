"""`_rollback_git`：**没有快照 ref 就不许销毁工作区**。

出处：外派逆向审抓到、我核过。本函数的语义是"用快照 ref **重建**工作区状态"；
ref 空了就重建不出任何东西，而照旧往下走是
`stash` + `checkout -- .` + `clean -fd` —— **销毁工作区、什么都不恢复，还返回 True**。

⚠️ **这条是从真实事故里长出来的**：当晚我两次跑测试时造的假任务
（没有 project_id ⇒ `repo_root_for` 返回**引擎本仓**）走进这条路径，
`stash push -u` + `clean -fd` 把开发者的工作区整个 stash 走。
两次都从 `stash@{0}` 捞回来了 —— **靠的是运气，不是保证**。
"""
import subprocess

import pytest

from singularity.scheduler import snapshot as S


def _git_repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=r, check=True, capture_output=True)
    (r / "a.txt").write_text("init", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True, capture_output=True)
    return r


def test_没有_ref_就拒绝销毁工作区(tmp_path, monkeypatch):
    """**正题**：工作区是脏的、而快照没有 ref ⇒ 不许 stash+clean。

    销毁了也恢复不回来 —— 那不如不做。
    """
    warns = []
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: warns.append(a))
    root = _git_repo(tmp_path)
    (root / "重要的改动.txt").write_text("这是开发者的未提交工作", encoding="utf-8")

    ok = S._rollback_git(S.Snapshot(id="b1", method="git", ref="", created_at=0.0), root)

    assert ok is False, "没 ref 还说回滚成功了"
    assert (root / "重要的改动.txt").exists(), "工作区被销毁了 —— 而什么都恢复不回来"
    assert any("no_snapshot_ref" in str(a) for a in warns), f"拒了却没出声：{warns}"


def test_有_ref_时照常回滚(tmp_path, monkeypatch):
    """对照：有 ref 的正常回滚不许被这次改动堵死。"""
    monkeypatch.setattr("singularity.scheduler.witness.warn", lambda *a, **k: None)
    from singularity.scheduler import config
    config.ensure_dirs()                          # `take()` 要往 SNAPSHOT_DIR 写
    root = _git_repo(tmp_path)
    snap = S.take("t1", repo_root=root)           # 干净工作区 ⇒ ref = HEAD
    (root / "后来加的.txt").write_text("x", encoding="utf-8")

    ok = S._rollback_git(S.Snapshot(id="t1", method="git", ref=snap.ref, created_at=0.0), root)

    assert ok is True
    assert not (root / "后来加的.txt").exists(), "回滚没把后加的文件清掉"
