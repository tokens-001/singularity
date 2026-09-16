"""惰性辐条的导入循环**必须互斥** —— 否则两个线程会撞出"半成品模块"。

## 症状（`alerts.jsonl` 里 43 次、跨三天、没人看）

    lazy_spoke_import_failed:_dispatch_exec:
    cannot import name '_load_skills_for_agent' from partially initialized module

## 形状（读码得到，不是推断）

`dispatcher.__getattr__` 按 `_LAZY_SPOKES` **依次** `import_module`，整个循环**没有互斥**：
  · T1 开始执行 `_dispatch_skills` —— 它此刻**在 `sys.modules` 里，但还没跑完**；
  · T2 也 `import_module("_dispatch_skills")`，拿到那个**半成品**，`hasattr` 为假，往后走；
  · T2 接着 `import_module("_dispatch_exec")` —— 它第 8 行是
    `from ..._dispatch_skills import _load_skills_for_agent`，而那名字在
    `_dispatch_skills` **第 57 行**才定义 ⇒ 正好是那句报错。

⚠️ **这里拷的是「互斥」这件事本身，不是去复现那个偶发**（单线程按三种导入顺序、
多线程 20 轮×8 线程都没复现出来）。做法：把 `import_module` 换成**会停一下**的替身，
让窗口足够宽，然后断言**同一时刻只有一个线程在这段里**。
变异验证：把 `with _LAZY_SPOKES_LOCK:` 拆掉 → 红。

⚠️ **这里只有这一条**。本来还写了第二条守"锁必须是 `RLock`（可重入）"——**删了**：
它是类型断言，而**我证明不了它会红**（一变异成 `Lock`，整个 pytest 就挂住不返回，
2026-09-16 实测挂了两回、只能手动 kill）。**证明不了会红的测试 = 死重量。**
`RLock` 那个选择的理由留在 `dispatcher._LAZY_SPOKES_LOCK` 的注释里。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import dispatcher as disp   # noqa: E402


class _Shim:
    """替身：**只**给 dispatcher 用的 importlib 视图（只实现它用到的那个方法）。

    ⚠️ 不要图省事去 `setattr(disp.importlib, "import_module", ...)` ——
    `disp.importlib` 指向的是**全局那个模块**，改了它 = 改了整个进程的导入，
    连 pytest 自己的收尾导入都会撞进来。
    """

    def __init__(self, fn):
        self._fn = fn

    def import_module(self, name):
        return self._fn(name)


def test_惰性辐条导入_同一时刻只有一个线程(monkeypatch):
    inside = 0
    peak = 0
    gate = threading.Lock()

    class _Stub:
        pass

    def fake_import(name):
        nonlocal inside, peak
        with gate:
            inside += 1
            peak = max(peak, inside)
        try:
            time.sleep(0.03)          # 把窗口撑开：没有互斥就一定会重叠
            return _Stub()            # 没有目标属性 ⇒ 循环会走完三条辐条
        finally:
            with gate:
                inside -= 1

    # ⚠️ **只换 `disp` 眼里那个 importlib**，别 `setattr(disp.importlib, ...)` ——
    # `disp.importlib` **就是全局那个模块**，那样会改掉整个进程的 `import_module`，
    # pytest 收尾时的导入也会撞进来（2026-09-16 实测：整个跑挂住）。
    monkeypatch.setattr(disp, "importlib", _Shim(fake_import))
    monkeypatch.setattr(disp.witness, "warn", lambda *a, **k: None)

    errs = []

    def worker():
        try:
            disp.__getattr__("这个属性谁都没有")
        except AttributeError:
            pass                      # 走完三条辐条后的正常结局
        except Exception as e:        # noqa: BLE001
            errs.append(e)

    # ⚠️ **必须 daemon**：变异成不可重入的锁时这些线程会永远卡着，
    # 而**非 daemon 线程会把进程钉住**（2026-09-16 实测：pytest 跑完了却不退出，
    # 工作区停在变异态，只能手动 kill）。
    ts = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=2.0)

    assert not any(t.is_alive() for t in ts), "有线程卡住了 —— 多半是锁不可重入 / 死锁"
    assert not errs, f"不该有别的异常：{errs}"
    assert peak == 1, (
        f"同一时刻有 {peak} 个线程在惰性导入段里 —— 没有互斥，"
        f"后进来的那个会拿到**半成品模块**（`partially initialized module` 就是这么来的）")
