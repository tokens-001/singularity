"""`dispatcher.__getattr__` 不许接 dunder —— 那才是 `lazy_spoke_import_failed` 的根。

出处：`~/OPEN.md` 🔴「`lazy_spoke_import_failed` 又活了 —— 09-16 那把锁没盖住全部路径」。
清单当时判的是"并发导入拿到半成品模块"，2026-09-16 加了 `_LAZY_SPOKES_LOCK`。
**锁没治好**（跨三天 80+ 次）。2026-09-18 探针实测（挂 `__getattr__` 记录器跑全量测试），
真机制是：

    from singularity.scheduler.dispatcher import X     ← 一条**普通的** import 语句
      → importlib 的 `_handle_fromlist` 先问 `hasattr(module, "__path__")`   ← 判断是不是包
        → 本模块有 PEP 562 的模块级 `__getattr__` ⇒ **这一问被转发进惰性循环**
          → 循环去导 `_dispatch_skills`；而 `_dispatch_skills` 模块体第 3 行自己就是一条
            `from singularity.scheduler.dispatcher import (...)` ⇒ 又一次问 `__path__`
            → **重入**：循环里 `import_module("_dispatch_skills")` 拿到**半成品**（它正在被导入）
              → 往下走 `_dispatch_exec` → 它第 8 行 `from _dispatch_skills import
                _load_skills_for_agent` → 名字还不存在 → 炸

**同一个线程自己绕回来** —— RLock 是同线程放行的，所以那把锁从原理上就挡不住这个形状。
实测一次全量测试要白跑 103 次（`__path__` 64 + `__test__` 26 + `__bases__` 13），
每一次都可能触发上面那条链。

这些测试**钉接线**：把门口那个 dunder 判据删掉，测试必须红。
"""
import sys

_SPOKES = (
    "singularity.scheduler._dispatch_skills",
    "singularity.scheduler._dispatch_exec",
    "singularity.scheduler._dispatch_crud",
)


def _unloaded(monkeypatch):
    """把三个辐条从 `sys.modules` 摘掉，退出时还原。

    ⚠️ **不摘干净这条测试就是假的**：辐条要是早被别的测试导进来了，
    "循环没去导它"和"它本来就在"分不出来 —— 判据就退化成"跑完没报错"。
    """
    saved = {m: sys.modules.pop(m) for m in _SPOKES if m in sys.modules}
    for m in _SPOKES:
        sys.modules.pop(m, None)
    monkeypatch.setattr(sys, "modules", sys.modules)   # 让 pytest 知道我们动过 sys.modules
    return saved


def test_dunder_探测不进惰性循环(monkeypatch):
    """`hasattr(dispatcher, "__path__")` 必须**立刻** AttributeError，不许碰辐条。

    ⚠️ 这**就是** `_handle_fromlist` 干的那一下，不是替身：每条
    `from singularity.scheduler.dispatcher import X` 都会先问它。

    把 `dispatcher.__getattr__` 开头那个 dunder 判据删掉 ⇒ 红
    （循环会跑起来，把 `_dispatch_skills` / `_dispatch_exec` 真导进来）。
    """
    from singularity.scheduler import dispatcher as D
    saved = _unloaded(monkeypatch)
    try:
        assert hasattr(D, "__path__") is False, "模块级 __getattr__ 不该造出一个 __path__"
        assert "singularity.scheduler._dispatch_skills" not in sys.modules, (
            "问一句 __path__ 就把辐条导进来了 —— _handle_fromlist 每次都会问这一句")
        assert "singularity.scheduler._dispatch_exec" not in sys.modules, (
            "更糟：它还会继续导 _dispatch_exec，而那正是会炸的那一跳")
    finally:
        sys.modules.update(saved)


def test_dunder_拒绝不影响真属性(monkeypatch):
    """**边界**：拒 dunder 不能顺手把真名字也拒了（辐条该转发还得转发）。"""
    from singularity.scheduler import dispatcher as D
    saved = _unloaded(monkeypatch)
    try:
        assert callable(D.load_agents), "真名字被 dunder 判据误伤"
        assert callable(D.dispatch), "跨辐条的真名字也被误伤"
    finally:
        sys.modules.update(saved)


def test_辐条里没有dunder会被挡掉():
    """万一哪天有辐条**真的导出**了 dunder，这条会红 —— 那时候判据得重新想，别硬撑。

    ⚠️ 第一版这条写成了"辐条里一个 dunder 都没有"，**当场被自己抓出是错的**：
    `_dispatch_exec` 有 `__annotate__` / `__conditional_annotations__` —— 那是
    **Python 3.14 (PEP 649) 给每个模块自动加的**，不是它导出的，也没有调用方会从
    `dispatcher` 上取。所以这里按"**解释器自动加的**"和"**辐条自己写的**"分开挡。
    """
    import importlib
    AUTO = {
        # 解释器/导入系统给每个模块挂的
        "__name__", "__doc__", "__package__", "__loader__", "__spec__", "__file__",
        "__builtins__", "__cached__", "__all__", "__dict__", "__path__",
        # Python 3.12+ / 3.14 的注解求值（PEP 649）
        "__annotate__", "__conditional_annotations__", "__annotations__",
        "__static_attributes__", "__firstlineno__",
    }
    for mod in _SPOKES:
        m = importlib.import_module(mod)
        dunders = [n for n in vars(m) if n.startswith("__") and n.endswith("__")
                   and n not in AUTO]
        assert not dunders, (
            f"{mod} 自己导出了 dunder {dunders} —— 门口那个判据会把它挡掉，"
            f"要么把它加进判据的例外，要么别让它当转发目标")
