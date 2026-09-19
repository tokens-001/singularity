"""`config.runtime_identity()` —— 「这个跑着的进程是谁」。

出处：`~/OPEN.md` 🟡「版本两套、都对不上发布 ⇒『真机跑的是哪版』在 git 层面不可回答」
（2026-09-20 查清：`pyproject` 那个号**全仓没人读**，真痛点是"跑着的进程不知道自己是谁"）。

⚠️ 这个函数**四条判断里有三条是 I/O**（git / 文件 mtime），所以用例不测"值是多少"，
测的是**两条会出错的逻辑**：

  ① `dirty_src` **只看 `src/`** —— `.qidian/` 是运行数据、天天在变，
     拿它当"脏"的话这个字段永远说"脏"，等于没说。**真跑起来第一天就会撞上这条。**
  ② `frontend_stale` 的**方向**和**秒级余量** —— 前者反了就是每轮都喊狼来了；
     后者没有的话，"提交完紧接着构建"会被报成落后（写这个函数的当天就撞上了）。

**变异**：把 ① 的路径参数 `--  src/` 去掉 ⇒ `test_qidian_里改了不算脏` 红；
把 ② 的 `- 90` 去掉 ⇒ `test_紧接着构建不算落后` 红。
"""
import subprocess
import time
from pathlib import Path

import pytest

from singularity.scheduler import config


def _git(repo: Path, *args: str, date: int | None = None) -> None:
    env = {"GIT_AUTHOR_DATE": f"{date} +0000", "GIT_COMMITTER_DATE": f"{date} +0000"} if date else {}
    import os
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=repo, check=True, capture_output=True,
                   env={**os.environ, **env})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """一个真 git 仓库（临时目录），`config.PROJECT_ROOT` 指过去。"""
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    _git(tmp_path, "init", "-q")
    return tmp_path


def _touch(p: Path, mtime: float) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    import os
    os.utime(p, (mtime, mtime))


DIST = Path("src/singularity/web/static/dist")
FE_SRC = Path("src/singularity/web/frontend/src")


# ═══════════════════════════════════════════════════════════════
# ① 拿不到就回 unknown —— **绝不抛**
# ═══════════════════════════════════════════════════════════════

def test_不是git仓库也照常返回不抛(tmp_path, monkeypatch):
    """无仓 / 打包分发 / 没装 git —— 都是正常处境，为它让启动失败是本末倒置。

    ⚠️ 判据是"**一条异常都不许冒出来**"，不是"字段长什么样" ——
    真机踩过的形状是"身份探测把服务搞挂"。
    """
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)     # 空目录，不是仓库
    got = config.runtime_identity()
    assert got["git"] == "unknown"
    assert got["dirty_src"] is None
    assert got["dist_built"] is None
    assert got["frontend_stale"] is None


# ═══════════════════════════════════════════════════════════════
# ② `dirty_src` 只看 `src/`
# ═══════════════════════════════════════════════════════════════

def test_干净的仓库不算脏(repo):
    _touch(repo / "src" / "a.py", time.time())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init", date=int(time.time()))
    assert config.runtime_identity()["dirty_src"] is False


def test_src里改了算脏(repo):
    _touch(repo / "src" / "a.py", time.time())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init", date=int(time.time()))
    (repo / "src" / "a.py").write_text("改了", encoding="utf-8")
    assert config.runtime_identity()["dirty_src"] is True


def test_qidian_里改了不算脏(repo):
    """🔴 **命门**：`.qidian/` 是本仓的**运行数据**（任务 / 告警 / 用量天天在写）。

    不排掉它的话，这个字段在**任何跑过一轮的机器上**恒为 True ——
    一个永远亮着的红，等于没有这个字段（本仓"常亮的假红换掉一个真红"那条）。
    """
    _touch(repo / "src" / "a.py", time.time())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init", date=int(time.time()))
    (repo / ".qidian" / "tasks").mkdir(parents=True)
    (repo / ".qidian" / "tasks" / "1.json").write_text("{}", encoding="utf-8")
    assert config.runtime_identity()["dirty_src"] is False, \
        "运行数据把 src/ 的『脏不脏』污染了 —— 这个字段从此恒亮"


# ═══════════════════════════════════════════════════════════════
# ③ `frontend_stale` 的方向 + 秒级余量
# ═══════════════════════════════════════════════════════════════

def _commit_frontend(repo: Path, when: int) -> None:
    _touch(repo / FE_SRC / "App.tsx", when)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fe", date=when)


def test_紧接着构建不算落后(repo):
    """**回归本仓当天踩的那一脚**：git 的 `%ct` 是整秒、mtime 有小数位 ——
    提交完紧接着构建，这两者会差几秒，严格 `<` 会把**刚构建完**的产物报成落后。

    这里故意让 dist 比提交**早 30 秒**（同一个构建流程里完全正常的顺序）。
    """
    when = int(time.time())
    _commit_frontend(repo, when)
    _touch(repo / DIST / "index.html", when - 30)
    assert config.runtime_identity()["frontend_stale"] is False, \
        "刚构建完就报落后 —— 这就是『常亮的假红』，喊几次狼来了真落后也没人看"


def test_落后两天的产物要点名(repo):
    """**正题**（09-15 真机那次：dist 停在 09-13，之后 17 个提交没进去，
    症状是"代码改了界面一点没变"，被误判成"修复没生效"）。"""
    when = int(time.time())
    _commit_frontend(repo, when)
    _touch(repo / DIST / "index.html", when - 2 * 86400)
    assert config.runtime_identity()["frontend_stale"] is True


def test_前端压根没提交过就不下结论(repo):
    """拿不到"前端最后一次提交"这个基准时回 None —— **不猜**。
    （本仓的规矩：说"过没过期"之前先答"依据从哪读出来的"。）"""
    _touch(repo / "src" / "a.py", time.time())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init", date=int(time.time()))
    _touch(repo / DIST / "index.html", time.time())
    assert config.runtime_identity()["frontend_stale"] is None
