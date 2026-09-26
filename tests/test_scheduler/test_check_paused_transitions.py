"""`_check_paused` 两次 `transition` 的返回值被丢掉 —— 拒了也报"已恢复"。

出处：Qoder CN 第二轮 #4（`docs/Qoder-审查-20260925-第二轮.md`），逐跳核过成立。
**在此之前 `_check_paused` 全仓一条测试都没有** —— 它只在别的测试的注释里被提到过。

**病**：`tracker.transition` **被拒时返回 `None`**，而两句调用都丢掉了返回值。
最刺眼的一支：worker 阻塞等恢复期间，收割者到点 → 先写 `cancels/<id>.json`
（`by:timeout`）→ 再 `transition(FAILED)`；`_TERMINAL_EXIT[FAILED]` **只放行 →PENDING**
⇒ 等用户点恢复、worker 走到最后那句 `transition(RUNNING)` 时**被拒** ⇒
函数照样报 True ⇒ **在一个已经被判死的任务上再发起一次完整模型调用**，没人收。

钉四件事：
  ① **拒了就是没恢复** —— 恢复那句被拒 ⇒ 返回 False（命门）；
  ② 进入 PAUSED 被拒 ⇒ **不阻塞**，立刻 False（否则白占一个 worker 位子，
     而"已死"看起来像"在跑"）；
  ③ **别修过头**：正常路径（无标记 / 正常暂停后恢复）一个字不许变；
  ④ 没有暂停标记时**根本不碰 tracker**（别把一次普通 turn 变成一次写盘）。
"""
import time
from types import SimpleNamespace

import pytest

from singularity.scheduler import _exec as E
from singularity.scheduler import config as C
from singularity.scheduler import tracker as T


def _task(mode="auto_edit"):
    return SimpleNamespace(id="t-paused-1", execution_mode=mode)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """把暂停/取消目录挪到 tmp —— 真跑起来绝不能碰生产 `.qidian/`。"""
    p, c = tmp_path / "pauses", tmp_path / "cancels"
    p.mkdir()
    c.mkdir()
    monkeypatch.setattr(C, "PAUSE_DIR", p)
    monkeypatch.setattr(C, "CANCEL_DIR", c)
    return p, c


def _transitions(monkeypatch, results):
    """把 `tracker.transition` 换成按序出结果的桩，并把每次调用记下来。"""
    calls = []

    def fake(task_id, new_status, *a, **k):
        calls.append(new_status)
        return results.pop(0) if results else None

    monkeypatch.setattr(T, "transition", fake)
    return calls


# ═══════════════════════════════════════════════════════════════
# ③ 别修过头：正常路径一个字不许变
# ═══════════════════════════════════════════════════════════════

def test_没有暂停标记时直接放行且不碰tracker(dirs, monkeypatch):
    calls = _transitions(monkeypatch, [])
    assert E._check_paused(_task()) is True
    assert calls == [], f"没暂停也去写 tracker —— 每次普通 turn 都多一次写盘：{calls}"


def test_正常暂停后恢复返回True(dirs, monkeypatch):
    """pause 文件被删 = 用户点了恢复 ⇒ 该返回 True，且状态真的切回 RUNNING。"""
    pause, _ = dirs
    (pause / "t-paused-1.json").write_text("{}", encoding="utf-8")
    calls = _transitions(monkeypatch, [SimpleNamespace(id="t-paused-1")] * 2)

    def fake_sleep(_s):
        (pause / "t-paused-1.json").unlink()      # 用户在这 1 秒里点了恢复

    monkeypatch.setattr(time, "sleep", fake_sleep)
    assert E._check_paused(_task()) is True
    assert [c.value for c in calls] == ["paused", "running"], calls


def test_暂停期间被取消返回False(dirs, monkeypatch):
    """**原有行为**：等在暂停里时车被叫停 ⇒ False（这条原来就对，别改坏）。"""
    pause, cancel = dirs
    (pause / "t-paused-1.json").write_text("{}", encoding="utf-8")
    _transitions(monkeypatch, [SimpleNamespace(id="t-paused-1")])

    def fake_sleep(_s):
        (cancel / "t-paused-1.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(time, "sleep", fake_sleep)
    assert E._check_paused(_task()) is False


# ═══════════════════════════════════════════════════════════════
# ② 进不去 PAUSED ⇒ 不阻塞
# ═══════════════════════════════════════════════════════════════

def test_进入PAUSED被拒时不阻塞直接返回False(dirs, monkeypatch):
    """变异：删掉 `if tracker_mod.transition(…PAUSED) is None:` 那个判断 ⇒ 本条红。

    ⚠️ 桩里那个 `sleep` **会抛**，是故意的：变异之后代码会走进阻塞循环，
    与其让 pytest 挂死等超时（看不出是红），不如让它在第一次 sleep 就炸掉。
    """
    pause, _ = dirs
    (pause / "t-paused-1.json").write_text("{}", encoding="utf-8")
    _transitions(monkeypatch, [None])          # 状态机拒绝：任务已是终态

    monkeypatch.setattr(time, "sleep",
                        lambda _s: (_ for _ in ()).throw(
                            AssertionError("PAUSED 被拒之后还在阻塞等恢复 —— 白占一个 worker 位子")))
    assert E._check_paused(_task()) is False


# ═══════════════════════════════════════════════════════════════
# ① 命门：恢复那句被拒 ⇒ 不许报"已恢复"
# ═══════════════════════════════════════════════════════════════

def test_恢复时被拒不许报已恢复(dirs, monkeypatch):
    """变异：把最后那句改成不看返回值（即 `tracker_mod.transition(...)`; `return True`）⇒ 本条红。

    这就是 Qoder #4 的正主：收割者在这段等待里按超时把任务判死了
    （`_TERMINAL_EXIT[FAILED]` 只放行 →PENDING ⇒ RUNNING 被拒），
    而函数照样报 True ⇒ 调用方往下走、**在一个已死的任务上再发一次完整模型调用**。
    """
    pause, _ = dirs
    (pause / "t-paused-1.json").write_text("{}", encoding="utf-8")
    # 第一次（进 PAUSED）成功；第二次（回 RUNNING）被拒 = 收割者抢先判了 FAILED
    calls = _transitions(monkeypatch, [SimpleNamespace(id="t-paused-1"), None])

    def fake_sleep(_s):
        (pause / "t-paused-1.json").unlink()

    monkeypatch.setattr(time, "sleep", fake_sleep)
    assert E._check_paused(_task()) is False, \
        "恢复被拒却报'已恢复' —— 接下来那次模型调用没人收，纯烧钱"
    assert [c.value for c in calls] == ["paused", "running"], calls


if __name__ == "__main__":            # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
