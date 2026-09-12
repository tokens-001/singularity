"""重活判据不能挂在"会归零的进程内计数器"上。

原来的判据是 `_consolidate_calls % 10 == 0`，而 `_consolidate_calls` 是**模块级变量、
后端每次重启归零** ⇒ 只要单个进程生命周期内的整合次数 < 10，这一步**永远不跑**。

实测（2026-09-12）：三个失败任务的 `attrs.abstraction` 全是 None（有个任务有 3414 字
真轨迹也没被抽象），那晚重启 4 次、没有一个进程跑到 10 —— 包括"经验分层抽象"在内
的整个重活块**从来没运行过一次**。

所以这里钉的核心不是"第 10 次会跑"，而是**"重启不清零"**：
进程内计数为零时，只要盘上攒够了，也照样该跑。
"""
import json
import time

from singularity.scheduler import _memory_consolidator as mc


def _state_file():
    return mc._heavy_state_path()


def _write_state(calls: int, last_heavy: float = 0.0):
    p = _state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"calls": calls, "last_heavy": last_heavy}), encoding="utf-8")


def test_restart_does_not_reset_the_count(tmp_path, monkeypatch):
    """**正题**：进程内计数是 0（刚重启），但盘上已经攒到 9 ⇒ 下一次就该跑。

    这正是原来那个 bug —— 老判据只看进程内计数，重启后从 0 开始，
    永远到不了 10。
    """
    monkeypatch.setattr(mc, "_consolidate_calls", 0)     # 模拟"刚重启"
    _write_state(calls=9, last_heavy=time.time() - 10)   # 盘上攒了 9 次
    assert mc._heavy_due() is True, "盘上攒够 10 次了却还不跑 —— 判据又挂回进程内计数了"


def test_fresh_state_does_not_run_immediately(tmp_path, monkeypatch):
    """全新目录：只记时间起点，别上来就烧一轮重活。"""
    monkeypatch.setattr(mc, "_consolidate_calls", 0)
    assert mc._heavy_due() is False
    st = json.loads(_state_file().read_text(encoding="utf-8"))
    assert st["calls"] == 1 and st["last_heavy"] > 0, st


def test_time_fallback_covers_a_machine_that_never_hits_ten(tmp_path, monkeypatch):
    """系统闲、一直攒不到 10 次 —— 时间兜底得把它捞起来。"""
    monkeypatch.setattr(mc, "_consolidate_calls", 0)
    _write_state(calls=1, last_heavy=time.time() - mc._HEAVY_EVERY_SEC - 60)
    assert mc._heavy_due() is True


def test_counter_accumulates_on_disk(tmp_path, monkeypatch):
    """没到点时要**把计数写回去** —— 不然攒不起来，改了个寂寞。"""
    monkeypatch.setattr(mc, "_consolidate_calls", 0)
    _write_state(calls=3, last_heavy=time.time() - 10)
    assert mc._heavy_due() is False
    assert json.loads(_state_file().read_text(encoding="utf-8"))["calls"] == 4


def test_after_running_the_counter_resets_and_time_moves(tmp_path, monkeypatch):
    """跑完要清计数、推进时间戳，否则下一次又立刻触发。"""
    monkeypatch.setattr(mc, "_consolidate_calls", 0)
    _write_state(calls=9, last_heavy=time.time() - 10)
    assert mc._heavy_due() is True
    after = json.loads(_state_file().read_text(encoding="utf-8"))
    assert after["calls"] == 0 and time.time() - after["last_heavy"] < 5
    assert mc._heavy_due() is False, "刚跑完不该立刻再跑"


def test_corrupt_state_file_does_not_crash(tmp_path, monkeypatch):
    """状态文件读坏了最多是"这次不跑"，不许把整合带崩。"""
    monkeypatch.setattr(mc, "_consolidate_calls", 0)
    p = _state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 不是 json", encoding="utf-8")
    assert mc._heavy_due() is False
