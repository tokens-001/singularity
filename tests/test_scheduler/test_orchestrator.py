"""Orchestrator tests — scheduling policy, monotonicity, chaos, benchmarks."""
import time, pytest
from singularity.scheduler.orchestrator import schedule_policy


class TestSchedulePolicy:
    """调度策略: 4维复合评分排序。"""

    @staticmethod
    def _task(tid, priority=0, wait_sec=0, depth=0, route_level="E",
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
    def _t(tid, priority=0, starvation=0, level="E", children=None):
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

    def test_level_bonus(self):
        a, b, c = self._t("a", level="D"), self._t("b", level="E+"), self._t("c", level="E")
        assert [t.id for t in schedule_policy([c, b, a])] == ["a", "b", "c"]

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
    def _t(tid, priority=0, starvation=0, level="E", children=None):
        return type("T", (), {"id": tid, "priority": priority, "starvation_score": starvation,
                              "route_level": level, "children": children or []})()

    def test_decompose_bad_input(self):
        from singularity.scheduler._exec import decompose
        assert decompose("not json") == []
        assert decompose("") == []
        assert decompose('{"x":1}') == []

    def test_decompose_valid(self):
        from singularity.scheduler._exec import decompose
        raw = '```json\n[{"desc": "task1", "suggested_level": "E", "depends_on_local_id": []}]\n```'
        r = decompose(raw)
        assert len(r) == 1
        assert r[0]["desc"] == "task1"

    def test_tracker_read_nonexistent(self):
        from singularity.scheduler.tracker import read_task
        assert read_task("nonexistent_99999") is None

    def test_schedule_policy_1k_under_50ms(self):
        tasks = [self._t(str(i), priority=i % 10, starvation=(1000 - i) * 0.1, level=["E", "E+", "D"][i % 3]) for i in range(1000)]
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
                "route_level": ["E", "E+", "D"][i % 3],
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
        subtasks = [{"desc": f"task {i}", "suggested_level": "E",
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

    def test_cache_10k_ops(self):
        from singularity.scheduler._cache import TTLStore
        c = TTLStore(ttl_seconds=60)
        start = time.perf_counter()
        for i in range(10000):
            c.set(str(i), {"v": i})
            c.get(str(i))
        elapsed = time.perf_counter() - start
        assert elapsed < 0.1, f"10k cache ops {elapsed:.3f}s > 0.1s"
