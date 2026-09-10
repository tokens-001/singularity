"""tracker 状态机：属性落地 + 写路径的原子性。

这些用例来自两个真找到的 bug：
  1. `route_role` 被 transition 写入、被 _exec 读取，但 **Task 根本没这个字段** ——
     `hasattr` 静默丢弃，角色提示词从来没注入过。
  2. `_observer_tools` 在锁外"读出来改完再 _write"，把调度刚 CAS 出的 ROUTED
     覆盖回 PENDING（实测复现的 lost update）。
"""
import dataclasses
from pathlib import Path

import pytest

from singularity.scheduler import config, tracker
from singularity.scheduler.tracker import Task, TaskStatus


@pytest.fixture
def qdir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def warns(monkeypatch):
    out = []
    monkeypatch.setattr("singularity.scheduler.witness.warn",
                        lambda *a, **k: out.append(a))
    return out


class TestAttrLanding:
    def test_route_role_is_a_real_field(self, qdir):
        assert "route_role" in {f.name for f in dataclasses.fields(Task)}

    def test_route_role_round_trips(self, qdir, warns):
        """写进去要读得出来 —— 曾经写侧静默丢、读侧永远拿到 ""。"""
        t = tracker.create("角色注入")
        tracker.transition(t.id, TaskStatus.PENDING,
                           route_locked=True, route_role="implementer")
        assert tracker.read_task(t.id).route_role == "implementer"
        assert not warns, f"不该有告警: {warns}"

    def test_old_task_file_without_route_role_loads(self, qdir):
        """旧任务文件没这个键，不能因为新增字段就加载失败。"""
        assert Task.from_dict({"id": "x", "description": "d"}).route_role == ""

    def test_unknown_kwarg_warns_instead_of_vanishing(self, qdir, warns):
        """Task 不认的键必须留痕 —— 静默丢弃正是 route_role 藏了一年的原因。"""
        t = tracker.create("拼错字段")
        tracker.transition(t.id, TaskStatus.PENDING, 拼错的字段="x")
        assert any("transition_unknown_kwargs" in str(w) for w in warns), warns

    def test_unknown_kwarg_on_cas_also_warns(self, qdir, warns):
        t = tracker.create("cas 拼错")
        tracker.cas(t.id, TaskStatus.PENDING, TaskStatus.ROUTED, 也不对="y")
        assert any("cas_unknown_kwargs" in str(w) for w in warns), warns


class TestWritePathAtomicity:
    def test_only_tracker_writes_task_files(self):
        """别处不许直接调 `tracker._write` —— 它不含锁。

        `_observer_tools` 曾经这么写：transition 之后 read 出对象、改 execution_mode、
        再 _write。这中间调度线程若 CAS 走了（PENDING→ROUTED），陈旧对象会把状态写回去，
        任务退回可调度态 → 可能被重复派发。改走 transition 的 kwargs 就原子了。
        """
        import singularity.scheduler as pkg

        offenders = []
        for py in Path(pkg.__file__).parent.rglob("*.py"):
            if py.name == "tracker.py":
                continue
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if "tracker._write(" in line or "tracker._write (" in line:
                    offenders.append(f"{py.name}:{i}")
        assert not offenders, (
            "有模块绕过 tracker._LOCK 直接写任务文件（lost update 风险）: "
            + ", ".join(offenders)
        )
