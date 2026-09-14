"""Orchestrator tests — scheduling policy, monotonicity, chaos, benchmarks."""
import time, pytest
from singularity.scheduler.orchestrator import schedule_policy


class TestSchedulePolicy:
    """调度策略: 4维复合评分排序。"""

    @staticmethod
    def _task(tid, priority=0, wait_sec=0, depth=0, route_level="any",
              starvation_score=0, children=None):
        return type("T", (), {
            "id": tid, "priority": priority, "wait_sec": wait_sec,
            "depth": depth, "route_level": route_level,
            "starvation_score": starvation_score,
            "children": children or [],
            "status": "pending",
        })()

    def test_empty(self):
        assert schedule_policy([]) == []

    def test_sort_by_priority(self):
        t1 = self._task("low", priority=1)
        t2 = self._task("high", priority=5)
        result = schedule_policy([t1, t2])
        assert result[0].id == "high"

    def test_starvation_prevention(self):
        t1 = self._task("old", starvation_score=100)
        t2 = self._task("new", priority=5, starvation_score=0)
        result = schedule_policy([t1, t2])
        assert result[0].id == "old"


class TestScheduleMonotonicity:
    @staticmethod
    def _t(tid, priority=0, starvation=0, level="any", children=None):
        return type("T", (), {
            "id": tid, "priority": priority, "starvation_score": starvation,
            "route_level": level, "children": children or [],
        })()

    def test_priority_ordering(self):
        a, b = self._t("a", priority=10), self._t("b", priority=1)
        assert schedule_policy([b, a])[0].id == "a"

    def test_starvation_prevents_hunger(self):
        a, b = self._t("a", starvation=100), self._t("b", starvation=1)
        assert schedule_policy([b, a])[0].id == "a"

    def test_level_no_longer_affects_ordering(self):
        # 两档后 level_bonus 已移除, E/E+/D 不影响排序
        a, b, c = self._t("a", priority=5), self._t("b", priority=3), self._t("c", priority=1)
        assert schedule_policy([c, b, a])[0].id == "a"

    def test_deterministic(self):
        tasks = [self._t(str(i), priority=i % 5, starvation=i) for i in range(10)]
        r1 = schedule_policy(list(tasks))
        r2 = schedule_policy(list(tasks))
        assert [t.id for t in r1] == [t.id for t in r2]

    def test_dependency_weight(self):
        a = self._t("a", children=["x", "y", "z"])
        b = self._t("b")
        assert schedule_policy([b, a])[0].id == "a"

    def test_empty_list(self):
        assert schedule_policy([]) == []


class TestChaosResilience:
    @staticmethod
    def _t(tid, priority=0, starvation=0, level="any", children=None):
        return type("T", (), {"id": tid, "priority": priority, "starvation_score": starvation,
                              "route_level": level, "children": children or []})()

    def test_decompose_bad_input(self):
        from singularity.scheduler._exec import decompose
        assert decompose("not json") == []
        assert decompose("") == []
        assert decompose('{"x":1}') == []

    def test_decompose_valid(self):
        from singularity.scheduler._exec import decompose
        raw = '```json\n[{"desc": "task1", "suggested_level": "any", "depends_on_local_id": []}]\n```'
        r = decompose(raw)
        assert len(r) == 1
        assert r[0]["desc"] == "task1"

    def test_tracker_read_nonexistent(self):
        from singularity.scheduler.tracker import read_task
        assert read_task("nonexistent_99999") is None

    def test_schedule_policy_1k_under_50ms(self):
        tasks = [self._t(str(i), priority=i % 10, starvation=(1000 - i) * 0.1, level="any") for i in range(1000)]
        t0 = time.perf_counter()
        schedule_policy(tasks)
        assert time.perf_counter() - t0 < 0.05


class TestBenchmark:
    """性能基准: 确保核心逻辑不退化。"""

    def test_schedule_policy_1k_tasks(self):
        tasks = []
        for i in range(1000):
            t = type("T", (), {
                "id": str(i), "priority": i % 10,
                "starvation_score": (1000 - i) * 0.1,
                "route_level": "any",
                "children": [],
                "status": "pending",
            })()
            tasks.append(t)
        start = time.perf_counter()
        result = schedule_policy(tasks)
        elapsed = time.perf_counter() - start
        assert len(result) == 1000
        assert elapsed < 0.2, f"1k task sort took {elapsed:.3f}s > 0.2s"

    def test_decompose_100_tasks(self):
        from singularity.scheduler._exec import decompose
        import json
        subtasks = [{"desc": f"task {i}", "suggested_level": "any",
                      "depends_on_local_id": [i - 1] if i > 0 else []}
                    for i in range(100)]
        raw = "```json\n" + json.dumps(subtasks) + "\n```"
        start = time.perf_counter()
        result = decompose(raw)
        elapsed = time.perf_counter() - start
        assert len(result) == 100
        assert elapsed < 0.05, f"100 task parse {elapsed:.3f}s > 0.05s"

    def test_topo_sort_50_nodes(self):
        from singularity.scheduler._planner import _topo_sort
        tasks = [{"local_id": i, "depends_on_local_id": [i - 1] if i > 0 else []}
                 for i in range(50)]
        start = time.perf_counter()
        order = _topo_sort(tasks)
        elapsed = time.perf_counter() - start
        assert len(order) == 50
        assert elapsed < 0.02, f"50 node topo {elapsed:.3f}s > 0.02s"


class TestOrphanScanIsPeriodic:
    """**水位触发**（2026-09-14，结构性那条的第一个落点）。

    孤儿探测原来**只在"队列要退出"那一刻**被调一次（`if not running_futures and
    not pending_batches`）—— 那只在流水线彻底空下来时成立；任务一个接一个来的
    时候**永远走不到那个分支**，探测等于没有。
    """

    def _mk(self, tid, status="running"):
        import json
        from singularity.scheduler import tracker
        (tracker.tasks_dir() / f"{tid}.json").write_text(
            json.dumps({"id": tid, "status": status, "description": "x"}), encoding="utf-8")

    def test_有人管的任务不算孤儿(self, monkeypatch):
        """**这条是新增的正确性要求**：带上活任务表之后，正常在跑的任务不能被报成孤儿。

        变异：删掉 `if tid in live: continue` → 红（把在跑的任务全报成孤儿）。
        """
        from singularity.scheduler import orchestrator as orch, witness
        monkeypatch.setattr(orch, "_orphans_warned", set())
        self._mk("t-live")
        warned = []
        monkeypatch.setattr(witness, "warn", lambda scope, msg, key="": warned.append(msg))

        class _T:
            id = "t-live"
        orch._warn_orphan_running({object(): (_T(), None, None, None, 0.0)}, {})
        assert warned == [], f"在跑的任务被报成孤儿：{warned}"

    def test_没人管的任务要报(self, monkeypatch):
        """反向：不在任何活表里的 RUNNING 任务**必须**报出来。"""
        from singularity.scheduler import orchestrator as orch, witness
        monkeypatch.setattr(orch, "_orphans_warned", set())
        self._mk("t-orphan")
        warned = []
        monkeypatch.setattr(witness, "warn", lambda scope, msg, key="": warned.append(msg))
        orch._warn_orphan_running({}, {})
        assert any("t-orphan" in w for w in warned), warned

    def test_pending_batches_里的也算有人管(self, monkeypatch):
        """`pending_batches`（已跑完、在等 merge）也是"有人管"的一种。"""
        from singularity.scheduler import orchestrator as orch, witness
        monkeypatch.setattr(orch, "_orphans_warned", set())
        self._mk("t-pending")
        warned = []
        monkeypatch.setattr(witness, "warn", lambda scope, msg, key="": warned.append(msg))
        orch._warn_orphan_running({}, {"t-pending": (None, None, None, None)})
        assert warned == [], f"等 merge 的任务被报成孤儿：{warned}"

    def test_循环每轮都做这件事(self, monkeypatch, tmp_path):
        """**接线**：`_run_queue_v3` 每轮真的要调它（带上活任务表）。
        变异：删掉循环里那次调用 → 红。"""
        from singularity.scheduler import orchestrator as orch
        calls = []
        monkeypatch.setattr(orch, "_warn_orphan_running",
                            lambda rf=None, pb=None: calls.append((rf, pb)))
        monkeypatch.setattr(orch, "_ORPHAN_SCAN_INTERVAL_S", 0)     # 不节流，保证这轮就调
        monkeypatch.setattr(orch.tracker, "ready_tasks", lambda exclude=None: [])
        monkeypatch.setattr(orch, "_auto_trigger_test_fix", lambda *a, **k: None)
        # 只让第一轮"看起来有活在跑" —— 否则循环会从"没活干"那个分支直接退出，
        # 根本走不到周期性对账那一步（第一版就是这么写错的：断言收到的是退出前那次）。
        state = {"n": 0}

        class _T:
            id = "t1"

        def fake_dispatch(dispatched, pool, agents, runner, rf, mq):
            if state["n"] == 0:
                rf[object()] = (_T(), None, None, None, 0.0)
            state["n"] += 1

        monkeypatch.setattr(orch, "_dispatch_ready", fake_dispatch)
        monkeypatch.setattr(orch, "_reap_futures",
                            lambda rf, pb, mq, runner, results: (rf.clear() or True))

        class _Pool:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def submit(self, *a, **k):
                raise AssertionError("不该派任务")
        monkeypatch.setattr(orch, "ThreadPoolExecutor", lambda **k: _Pool())
        orch._run_queue_v3({}, 1)

        assert calls, "循环里没有周期性对账那一步"
        assert calls[0] == ({}, {}), f"应带上活任务表（空表也要带）：{calls[0]}"
