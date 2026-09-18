"""tracker 状态机：属性落地 + 写路径的原子性。

这些用例来自两个真找到的 bug：
  1. `route_role` 被 transition 写入、被 _exec 读取，但 **Task 根本没这个字段** ——
     `hasattr` 静默丢弃，角色提示词从来没注入过。
  2. `_observer_tools` 在锁外"读出来改完再 _write"，把调度刚 CAS 出的 ROUTED
     覆盖回 PENDING（实测复现的 lost update）。
"""
import dataclasses
import json
from pathlib import Path

import pytest

from singularity.scheduler import config, tracker
from singularity.scheduler.tracker import Task, TaskStatus


@pytest.fixture
def qdir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    # `_TASK_WARNED` 是**进程级**去重集合（见 `tracker._warn_task_once`）——
    # 不清的话，上一个用例报过的记号会让下一个用例"没出声"，红得莫名其妙。
    monkeypatch.setattr(tracker, "_TASK_WARNED", set())
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


# ═══════════════════════════════════════════════════════════════
# S5：读路径不许静默吞错（2026-09-18 独立复现，2026-09-19 修）
#
# 四处读路径（`read_task` / `ready_tasks` / `recover` / `_load_all_tasks`）原来
# 一律 `except (JSONDecodeError, TypeError, ValueError): return None / continue`，
# **零留痕**（实测：tmp 里 `alerts.jsonl` 从头到尾没被创建）。
#
# 最毒的一环：`Task.from_dict` 结尾是裸 `cls(**d)`，**不丢未知键** ——
# 一条 RUNNING 任务只要多一个未知键就读成 None ⇒ `recover()` **永远不碰它**
# ⇒ 它**永远是 RUNNING 的幽灵**，同时在任务列表里**根本不存在**。
# ═══════════════════════════════════════════════════════════════

class TestBrokenTaskFileLeavesATrace:

    def _seed(self, qdir, tid: str, body: str):
        d = qdir / "tasks"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{tid}.json"
        p.write_text(body, encoding="utf-8")
        return p

    def _seed_running_with_unknown_key(self, qdir, tid="ghost"):
        t = Task(id=tid, description="x", status=TaskStatus.RUNNING)
        body = t.to_dict()
        body["某个新版本才有的键"] = 1        # 回滚 / 手工改盘 / 迁移写一半的产物
        return self._seed(qdir, tid, json.dumps(body, ensure_ascii=False))

    def test_坏文件读成None但要有备份(self, qdir, warns):
        p = self._seed(qdir, "broken", "{not json")
        assert tracker.read_task("broken") is None
        assert any("broken.json" in str(w) for w in warns), f"读坏一声不吭: {warns}"
        assert (qdir / "tasks" / "broken.json.corrupt").exists(), \
            "没有留原始字节的备份 —— 出了事连现场都没了"

    def test_坏文件不刷屏(self, qdir, warns):
        """**命门**：`ready_tasks` 每 2 秒把全部任务文件扫一遍。

        每读一次报一次 = 一份坏文件每 2 秒刷一条告警 —— 那就是清单上
        「留痕写成每轮一条 = 又一条糊筛子的告警」（`drain_dep_blocked` 一天 1850 条）。
        变异：把 `_read_task_file` 开头那道 `is_quarantined` 闸门删掉 → 红。
        """
        self._seed(qdir, "broken", "{not json")
        for _ in range(5):
            tracker.ready_tasks()
            tracker._load_all_tasks()
        n = sum(1 for w in warns if "broken.json" in str(w))
        assert n == 1, f"同一份坏文件报了 {n} 次 —— 又糊了一条筛子"

    def test_未知键不该让整条任务消失(self, qdir, warns):
        """🔴 S5 里最毒的一环。变异：删掉 `from_dict` 里那段 `extra` 过滤 → 红。"""
        self._seed_running_with_unknown_key(qdir)
        got = tracker.read_task("ghost")
        assert got is not None, "多一个未知键 ⇒ 整条任务在系统里消失了（文件明明在盘上）"
        assert got.status is TaskStatus.RUNNING
        assert any("task_unknown_keys" in str(w) for w in warns), \
            f"键被丢了却不说一声 ⇒ 下次整份写回时静默丢数据: {warns}"

    def test_幽灵任务真被_recover_捞回去(self, qdir, warns):
        """**接线**：光"`read_task` 看得见"不够 —— 那条永远 RUNNING 的幽灵
        要真的被回收。`recover()` 是唯一会碰它的地方。

        变异：删掉 `from_dict` 里那段 `extra` 过滤 → 红（recover 数不到它）。
        """
        self._seed_running_with_unknown_key(qdir)
        assert tracker.recover() == 1, \
            "RUNNING 幽灵没被回收 —— 它会一直挂在 RUNNING，界面上也查不到"
        assert tracker.read_task("ghost").status is TaskStatus.PENDING

    def test_读不出来的任务不参与调度(self, qdir, warns):
        """另一半：认不出来就别调度它（`ready_tasks` 原来靠 `continue` 也是这个效果，
        这里钉住"修完还是不让它上"）。"""
        self._seed(qdir, "broken", "{not json")
        assert [t.id for t in tracker.ready_tasks()] == []

    def test_正常任务一个告警都不许有(self, qdir, warns):
        """命门：干净的仓库里读一遍，**不许**报任何东西。"""
        d = qdir / "tasks"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ok.json").write_text(
            json.dumps(Task(id="ok", description="x").to_dict(), ensure_ascii=False),
            encoding="utf-8")
        assert tracker.read_task("ok") is not None
        tracker.ready_tasks()
        tracker._load_all_tasks()
        assert warns == [], f"干净任务被误报: {warns}"
