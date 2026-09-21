"""「人按了停，循环就不该被自己拉回来」（2026-09-21 用户拍板修）。

## 症状（真机实测）

`POST /api/loop/stop` **停不住** —— 观察者建/路由任务那句末尾写着
「确保调度循环在跑」：

    if not _hooks.loop_status().get("running"):
        _hooks.start_loop(concurrent=2)

它**只问"在不在跑"，不问"谁停的"** ⇒ 人一按停，观察者下一句就拉回来。
实测：**19:15 停、19:17 又被拉起来派活**；`SIGTERM` 也杀不动，真停只能 `kill -9`
（没有停观察者的接口）。

## 改法

在 `_hooks` 里记一笔「**是人停的**」，让那句守卫问一句再决定：

  · `/api/loop/stop` 和观察者的 `control_loop("stop")` ⇒ `note_human_stop()`
  · `/api/loop/start` 和 `control_loop("start")` ⇒ `note_human_start()`（显式开 ⇒ 自动拉起重新生效）
  · ⚠️ **`_graceful_shutdown`（进程退出）不记** —— 那不是"人停的"

## 每一条都要有对照（本文件最重要的部分）

**只验"人停之后不拉"是不够的** —— 那用一个 `return` 就能骗过，
而那样会把「循环意外死了没人管」放进来。所以第 2 条钉的是**反方向**：
**没人停的时候，守卫必须照旧拉**。

变异验证（删哪一行会红）：
  · 去掉守卫里的 `and not _hooks.human_stopped()` → `test_人停过之后_…` 红；
  · 把守卫整个删掉（恒不拉） → **`test_没人停_循环意外死了照旧拉起来` 红**（对照那两条）；
  · `api_loop_stop` 去掉 `note_human_stop()` → `test_经HTTP停也算人停` 红；
  · `api_loop_start` 去掉 `note_human_start()` → `test_经HTTP开也算显式开` 红。

⚠️ **最后一条是补的，而且是被变异逼出来的**：原来只测了直接调 `_hooks`，
所以把 `api_loop_start` 那句删掉**测试照样绿** —— 而我在这个 docstring 里
**已经写了"删它会红"**。**写下没验过的变异结论，本身就是一种假绿。**
"""
import pytest

from singularity.scheduler import _hooks
from singularity.scheduler import _observer_tools as ot


@pytest.fixture(autouse=True)
def _干净状态(monkeypatch):
    """每条用例从"没人停过"开始 —— 这是个模块级全局量，不隔离会互相污染。"""
    monkeypatch.setattr(_hooks, "_stopped_by_human", False)
    yield


@pytest.fixture
def 拉起来记录(monkeypatch):
    """接住 `_hooks.start_loop` —— 断言"到底拉没拉"。"""
    got = []
    monkeypatch.setattr(_hooks, "start_loop", lambda concurrent=1: got.append(concurrent) or True)
    return got


def _让观察者建一个任务(monkeypatch, 循环在跑: bool):
    """把 `_tool_create_task` 的桩搭到"只差最后那句守卫"。"""
    from singularity.scheduler import tracker
    monkeypatch.setattr(tracker, "create", lambda desc: type("T", (), {"id": "t1"})())
    monkeypatch.setattr(tracker, "transition", lambda *a, **k: None)
    monkeypatch.setattr(_hooks, "loop_status", lambda: {"running": 循环在跑, "concurrent": 2})
    return ot._tool_create_task("写个模块", level="any")


# ═══════════════ ① 人停过 ⇒ 不拉 ═══════════════

def test_人停过之后_观察者不再自己拉循环(monkeypatch, 拉起来记录):
    """🔴 这条就是"停不住"本身：停完，观察者建个任务又把它拉起来。"""
    _hooks.note_human_stop()

    _让观察者建一个任务(monkeypatch, 循环在跑=False)

    assert 拉起来记录 == [], (
        "人明确按了停，观察者不该把它拉回来 —— 那正是'停不住、只能 kill -9'的根因")


def test_经HTTP停也算人停(monkeypatch):
    """**接线**：`/api/loop/stop` 那条路也得记上 —— 只改观察者自己那句是够不着的。"""
    from singularity.web import app as web_app
    monkeypatch.setattr(web_app, "stop_loop", lambda: True)
    monkeypatch.setattr(_hooks, "_stopped_by_human", False)

    with web_app.app.test_client() as c:
        r = c.post("/api/loop/stop")

    assert r.status_code == 200
    assert _hooks.human_stopped() is True, "HTTP 停完没记上 ⇒ 观察者下一句照样拉回来"


# ═══════════════ ② 对照：没人停 ⇒ 照旧拉（**别把守卫整个废掉**）═══════════════

def test_没人停_循环意外死了照旧拉起来(monkeypatch, 拉起来记录):
    """**这条是反方向的对照，比第 1 条还重要**。

    去掉它，改法可以退化成"守卫恒不拉" —— 那时第 1 条照样绿，
    而**循环意外死掉就再也没人管了**（那正是这个守卫当初存在的理由）。
    """
    _让观察者建一个任务(monkeypatch, 循环在跑=False)

    assert 拉起来记录 == [2], (
        "没人按过停 ⇒ 循环死了必须照旧拉起来 —— 别用一个 return 把守卫整个废掉")


def test_循环本来就活着时_不重复拉(monkeypatch, 拉起来记录):
    """**对照**：循环在跑就别多事（这条是原来的行为，一个字不该变）。"""
    _让观察者建一个任务(monkeypatch, 循环在跑=True)

    assert 拉起来记录 == []


# ═══════════════ ③ 显式开 ⇒ 恢复自动拉起 ═══════════════

def test_显式开过之后_自动拉起重新生效(monkeypatch, 拉起来记录):
    """人又把循环开起来了 ⇒ 回到正常。否则"停过一次"就永久禁用了这个守卫。"""
    _hooks.note_human_stop()
    _hooks.note_human_start()

    _让观察者建一个任务(monkeypatch, 循环在跑=False)

    assert 拉起来记录 == [2], "显式开过之后，自动拉起必须重新生效"


def test_经HTTP开也算显式开(monkeypatch):
    """**接线**：和"经 HTTP 停"那条对称。

    ⚠️ 这条是**补出来的**：原来只测了直接调 `_hooks.note_human_start()`，
    于是把 `api_loop_start` 里的 `note_human_start()` 删掉**测试照样绿** ——
    我在文件头还写了一句"删它会红"，**那句话当时是空话**（变异 ④ 没红才发现）。
    """
    from singularity.web import app as web_app
    monkeypatch.setattr(web_app, "start_loop", lambda concurrent=1: True)
    _hooks.note_human_stop()                      # 先造成"停过"的状态

    with web_app.app.test_client() as c:
        r = c.post("/api/loop/start", json={"concurrent": 1})

    assert r.status_code == 200
    assert _hooks.human_stopped() is False, (
        "HTTP 显式开完没把标记清掉 ⇒ 这个守卫就永久失效了（停过一次就再也拉不起来）")


def test_观察者自己的stop也算人停(monkeypatch):
    """观察者 `control_loop("stop")` 是**照着人的意思**停的 —— 它自己也不能下一句就拉回来。"""
    monkeypatch.setattr(_hooks, "stop_loop", lambda: True)
    monkeypatch.setattr(_hooks, "loop_status", lambda: {"running": False, "concurrent": 2})
    monkeypatch.setattr(_hooks, "_stopped_by_human", False)

    ot._tool_control_loop("stop")

    assert _hooks.human_stopped() is True
