"""`config.runtime_identity()` —— 「这个跑着的进程是谁」。

出处：`~/OPEN.md` 🟡「版本两套、都对不上发布 ⇒『真机跑的是哪版』在 git 层面不可回答」
（2026-09-20 查清：`pyproject` 那个号**全仓没人读**，真痛点是"跑着的进程不知道自己是谁"）。

⚠️ 这个函数**四条判断里有三条是 I/O**（git / 文件 mtime），所以用例不测"值是多少"，
测的是**两条会出错的逻辑**：

  ① `dirty_src` **只看 `src/`** —— `.qidian/` 是运行数据、天天在变，
     拿它当"脏"的话这个字段永远说"脏"，等于没说。**真跑起来第一天就会撞上这条。**
  ② `frontend_stale` **比的是源码文件的 mtime，不是"最后一次提交"的时间** ——
     用提当代proxy 会把**刚构建完**的 dist 报成落后（正常顺序是
     "改 → build → 提交"，提交必然晚几分钟）。**写这个函数的当天就被咬了一次。**

**变异**：把 ① 的路径参数 `--  src/` 去掉 ⇒ `test_qidian_里改了不算脏` 红；
把 ② 换回"比最后一次提交" ⇒ `test_源码没动过_只是提交了_不算落后` 红。
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
# ③ `frontend_stale`：**比源码文件的 mtime，不比提交时间**
#
# 老判据（本仓 CLAUDE.md 里那条）是拿 `git log -1 --format=%ad -- frontend/src`
# 跟 dist 比。**写这个函数的当天就被它咬了一次**：正常顺序是"改 → build → 提交"，
# 提交**必然晚于构建几分钟** ⇒ 把刚构建完的 dist 报成落后。
# 补了个 90 秒余量还是假红（那次差 2 分钟）—— 因为病根是**拿"提交时间"当"源码变了"的代理**：
# 提交不改变源码内容，它凭什么让构建作废。
# ═══════════════════════════════════════════════════════════════

def test_源码没动过_只是提交了_不算落后(repo):
    """🔴 **回归那一脚**（判据换掉之前，这条会红）。

    顺序完全正常：先 `build`（dist=now），**几分钟后**才 `git commit`。
    ⇒ 不算落后。**拿提交时间当基准就会把这条读成落后。**
    """
    now = time.time()
    _touch(repo / FE_SRC / "App.tsx", now - 300)     # 5 分钟前改的源码
    _touch(repo / DIST / "index.html", now - 240)    # 4 分钟前构建的（晚于源码 ✓）
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fe")                # 现在才提交
    assert config.runtime_identity()["frontend_stale"] is False, \
        "只是提交晚了几分钟 —— 这就是『常亮的假红』，喊几次狼来了真落后也没人看"


def test_源码改过没重建就算落后(repo):
    """**正题**（09-15 真机那次：dist 停在 09-13，之后 17 个提交没进去，
    症状是"代码改了界面一点没变"，被误判成"修复没生效"）。"""
    now = time.time()
    _touch(repo / DIST / "index.html", now - 2 * 86400)   # 两天前的构建
    _touch(repo / FE_SRC / "App.tsx", now)                # 刚改的源码
    assert config.runtime_identity()["frontend_stale"] is True


def test_压根没构建过也算落后(repo):
    """`dist/` 不存在 = 界面压根起不来（`app.py` 读 `static/dist/index.html`），
    比"旧"更严重 —— 不是"不知道"，是**确定跟不上**。"""
    _touch(repo / FE_SRC / "App.tsx", time.time())
    assert config.runtime_identity()["frontend_stale"] is True
    assert config.runtime_identity()["dist_built"] is None


def test_前端源码目录都没有就不下结论(repo):
    """拿不到"前端源码"这个基准时回 None —— **不猜**。
    （本仓的规矩：说"过没过期"之前先答"依据从哪读出来的"。）"""
    _touch(repo / "src" / "a.py", time.time())
    _touch(repo / DIST / "index.html", time.time())
    assert config.runtime_identity()["frontend_stale"] is None
