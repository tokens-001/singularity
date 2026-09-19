"""「集成」的账 —— **进仓了几个任务 / 没进仓几个**。

🔴 来历（2026-09-20 `round-20260920b` 真机）：一轮跑完，各任务往
`refs/qidian/pending/<task_id>` 锚了 **5654 行**产物，而**合并进项目仓的代码是 0 行**
（主分支只有 `.gitignore` / `README.md` / `pyproject.toml`）——
**界面上一个字都看不出来**。「有可打捞的产物」那个标只挂在**单张任务卡**上
（`TaskCard`），没人把项目级的账加起来。这块就是那个账。

钉三件事：
  ① **进仓 / 没进仓分得清** —— 判据是"ref 还在不在" + 提交标题里的任务号；
  ② **二进制文件不能炸掉解析** —— `git --numstat` 对二进制给的是 `-` 不是数字，
     真机项目里就有图；`int('-')` 一抛整个面板就没了；
  ③ **详情接口真的把这块带上了**（接线）—— 删掉 `project_detail` 里那行赋值就红。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from singularity.scheduler import config                    # noqa: E402
from singularity.scheduler import project as proj_mod        # noqa: E402
from singularity.scheduler import _api_projects as api       # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projs")
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, subject: str, files: dict[str, bytes]) -> str:
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD").strip()


def _make_project(tmp_path, monkeypatch, name: str):
    """建项目 + 建它的仓，返回 (项目对象, 仓路径)。"""
    _isolate(tmp_path, monkeypatch)
    p = proj_mod.create(name=name)
    repo = proj_mod.repo_dir(p.id)
    _init_repo(repo)
    _commit(repo, "init project repo", {".gitignore": b"__pycache__/\n"})
    return p, repo


def _body(n: int) -> bytes:
    return ("\n".join(f"line {i}" for i in range(n)) + "\n").encode()


def test_进仓与没进仓分得清(tmp_path, monkeypatch):
    p, repo = _make_project(tmp_path, monkeypatch, "int-demo")

    # ① 进了仓的：提交直接躺在 HEAD 上（标题带任务号）
    #    ⚠️ 同时塞一个**二进制文件** —— `--numstat` 对它给的是 `-`，
    #    解析里那句 `isdigit()` 就是为它挡的。
    _commit(repo, "agent changes in 111_any", {
        "a.py": _body(10),
        "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x01binary",
    })

    # ② 没进仓的：提交最后被锚在 pending ref 上（成功合并那条路会 release 掉它）
    sha2 = _commit(repo, "agent changes in 222_any", {"b.py": _body(7)})
    _git(repo, "update-ref", "refs/qidian/pending/222", sha2)
    #    HEAD 退回来，模拟"这个任务的提交**没**进主分支"
    _git(repo, "reset", "-q", "--hard", "HEAD~1")

    # ③ 别人的任务号 —— **不该混进这个项目的账**
    sha3 = _commit(repo, "agent changes in 999_any", {"c.py": _body(3)})
    _git(repo, "update-ref", "refs/qidian/pending/999", sha3)
    _git(repo, "reset", "-q", "--hard", "HEAD~1")

    p.task_ids = ["111", "222"]
    p.lineage = [
        {"action": "integration_merge", "ok": True, "tests_ran": False},
        {"action": "machine_checks", "ran": 10, "passed": 0},
    ]

    out = api.project_integration(p)

    assert out["merged"]["tasks"] == ["111"], "进仓的任务认错了"
    assert out["merged"]["commits"] == 1
    assert out["merged"]["insertions"] == 10, "二进制的 `-` 那行被算进去了？应只数 a.py 的 10 行"
    assert out["not_merged"]["tasks"] == ["222"], "没进仓的任务认错了（泄漏了别的项目号？）"
    assert out["not_merged"]["insertions"] == 7
    assert out["not_merged"]["refs"][0]["sha"] == sha2[:7]
    assert out["tests_ran"] is False, "集成没跑测试这件事必须报出来"
    assert out["machine_checks"] == {"ran": 10, "passed": 0}


def test_没有锚时没进仓是空的(tmp_path, monkeypatch):
    """对照组：一个 ref 都没有 ⇒ 没进仓 0 个。**不能是"全都没进仓"**。"""
    p, repo = _make_project(tmp_path, monkeypatch, "int-clean")
    _commit(repo, "agent changes in 111_any", {"a.py": _body(5)})
    p.task_ids = ["111"]
    p.lineage = []

    out = api.project_integration(p)

    assert out["not_merged"]["tasks"] == []
    assert out["not_merged"]["insertions"] == 0
    assert out["tests_ran"] is None, "没记录 和 没跑 是两件事，别混"
    assert out["machine_checks"] is None


def test_详情接口带上了这块账(tmp_path, monkeypatch):
    """**接线**：详情接口必须把这块钱带上。删掉 `project_detail` 里那行赋值 → 本用例红。"""
    p, repo = _make_project(tmp_path, monkeypatch, "int-wired")
    _commit(repo, "agent changes in 111_any", {"a.py": _body(4)})
    sha = _commit(repo, "agent changes in 222_any", {"b.py": _body(9)})
    _git(repo, "update-ref", "refs/qidian/pending/222", sha)
    _git(repo, "reset", "-q", "--hard", "HEAD~1")

    p.task_ids = ["111", "222"]
    proj_mod.save(p)

    detail, code = api.project_detail(p.id)
    assert code == 200
    assert "integration" in detail, "详情里没有 integration —— 接线断了"
    assert detail["integration"]["merged"]["tasks"] == ["111"]
    assert detail["integration"]["not_merged"]["tasks"] == ["222"]


if __name__ == "__main__":            # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
