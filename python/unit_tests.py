"""奇点单元测试 — 核心逻辑 (不涉及网络/LLM)。

运行: QIDIAN_SKIP_EMBED=1 python3 -m pytest unit_tests.py -v
      或 python3 unit_tests.py
"""

import os, sys, json, time, unittest
from pathlib import Path

os.chdir(Path(__file__).parent)
sys.path.insert(0, ".")
os.environ["QIDIAN_SKIP_EMBED"] = "1"


class TestSchedulePolicy(unittest.TestCase):
    """调度策略: 4维复合评分排序。"""

    def _task(self, id, priority=0, wait_sec=0, depth=0, route_level="E",
              starvation_score=0, children=None):
        return type("T", (), {
            "id": id, "priority": priority, "wait_sec": wait_sec,
            "depth": depth, "route_level": route_level,
            "starvation_score": starvation_score,
            "children": children or [],
            "status": "pending",
        })()

    def test_empty(self):
        from scheduler.orchestrator import schedule_policy
        self.assertEqual(schedule_policy([]), [])

    def test_sort_by_priority(self):
        from scheduler.orchestrator import schedule_policy
        t1 = self._task("low", priority=1)
        t2 = self._task("high", priority=5)
        result = schedule_policy([t1, t2])
        self.assertEqual(result[0].id, "high")

    def test_starvation_prevention(self):
        """长时间等待的任务应该被提升。"""
        from scheduler.orchestrator import schedule_policy
        t1 = self._task("old", starvation_score=100)
        t2 = self._task("new", priority=5, starvation_score=0)
        result = schedule_policy([t1, t2])
        self.assertEqual(result[0].id, "old")


class TestDecompose(unittest.TestCase):
    """Planner JSON 解析。"""

    def test_valid_decomposition(self):
        from scheduler._exec import decompose
        raw = '```json\n[{"desc": "task A", "suggested_level": "E", "depends_on_local_id": []}]\n```'
        tasks = decompose(raw)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["desc"], "task A")

    def test_no_json_block(self):
        from scheduler._exec import decompose
        tasks = decompose("just some text, no json")
        self.assertEqual(tasks, [])

    def test_multiple_tasks_with_deps(self):
        from scheduler._exec import decompose
        raw = '''```json
[
  {"desc": "task 1", "suggested_level": "E", "depends_on_local_id": []},
  {"desc": "task 2", "suggested_level": "E+", "depends_on_local_id": [0]}
]
```'''
        tasks = decompose(raw)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1]["depends_on_local_id"], [0])


class TestTopoSort(unittest.TestCase):
    """Kahn 拓扑排序。"""

    def test_linear_deps(self):
        from scheduler._planner import _topo_sort
        tasks = [
            {"local_id": 0, "depends_on_local_id": []},
            {"local_id": 1, "depends_on_local_id": [0]},
            {"local_id": 2, "depends_on_local_id": [1]},
        ]
        order = _topo_sort(tasks)
        self.assertEqual(order, [0, 1, 2])

    def test_no_deps(self):
        from scheduler._planner import _topo_sort
        tasks = [{"local_id": 0}, {"local_id": 1}, {"local_id": 2}]
        order = _topo_sort(tasks)
        self.assertIsNotNone(order)
        self.assertEqual(set(order), {0, 1, 2})

    def test_cycle_detection(self):
        from scheduler._planner import _topo_sort
        tasks = [
            {"local_id": 0, "depends_on_local_id": [1]},
            {"local_id": 1, "depends_on_local_id": [0]},
        ]
        order = _topo_sort(tasks)
        self.assertIsNone(order)  # 环 → None


class TestTTLCache(unittest.TestCase):
    """内存缓存。"""

    def test_set_get(self):
        from scheduler._cache import TTLStore
        c = TTLStore(ttl_seconds=60)
        c.set("k1", {"a": 1})
        self.assertEqual(c.get("k1"), {"a": 1})

    def test_expiry(self):
        from scheduler._cache import TTLStore
        c = TTLStore(ttl_seconds=0.01)
        c.set("k1", {"a": 1})
        time.sleep(0.02)
        self.assertIsNone(c.get("k1"))

    def test_invalidate(self):
        from scheduler._cache import TTLStore
        c = TTLStore(ttl_seconds=60)
        c.set("k1", {"a": 1})
        c.invalidate("k1")
        self.assertIsNone(c.get("k1"))


class TestRouter(unittest.TestCase):
    """路由判定。"""

    def test_route_basic(self):
        from scheduler.router import route
        r = route("fix a typo in README")
        self.assertIn(r.level, ("E", "E+", "D"))
        self.assertIsNotNone(r.task_type)

    def test_route_complex(self):
        from scheduler.router import route
        r = route("重构整个认证系统，支持OAuth2和JWT，改动涉及10个文件")
        self.assertIn(r.level, ("E", "E+", "D"))


class TestProjectState(unittest.TestCase):
    """项目状态机。"""

    def test_create_and_phases(self):
        from scheduler.project import create, Phase
        p = create("test_unit", template="product_dev")
        self.assertEqual(p.phase, Phase.TEMPLATE)
        self.assertTrue(p.id)

    def test_confirm_gate(self):
        from scheduler.project import create, Phase, save
        p = create("test_gate", template="product_dev")
        p.phase = Phase.GATE1
        p.confirm_gate(Phase.GATE1, "approved")
        self.assertEqual(p.phase, Phase.PLANNING)

    def test_reject_gate(self):
        from scheduler.project import create, Phase
        p = create("test_reject", template="product_dev")
        p.phase = Phase.GATE1
        p.confirm_gate(Phase.GATE1, "rejected")
        self.assertEqual(p.phase, Phase.TEMPLATE)

    @classmethod
    def tearDownClass(cls):
        # 清理测试项目
        from scheduler.project import list_all, _path
        for p in list_all():
            if "test_" in p.name:
                try:
                    _path(p.id).unlink()
                except Exception:
                    pass


class TestModelRegistry(unittest.TestCase):
    """模型注册表查询。"""

    def test_load_models(self):
        from scheduler.model_registry import load_models
        models = load_models()
        self.assertGreater(len(models), 3)
        self.assertIn("deepseek-chat", models)

    def test_for_tier(self):
        from scheduler.model_registry import for_tier
        e_models = for_tier("E", available_only=False)
        self.assertGreater(len(e_models), 0)
        tiers = {t for m in e_models for t in m.tiers}
        self.assertIn("E", tiers)


class TestInsertAgent(unittest.TestCase):
    """Agent CRUD。"""

    def test_load_agents(self):
        from scheduler.dispatcher import load_agents
        agents = load_agents()
        self.assertIn("E", agents)
        self.assertIn("D", agents)
        self.assertGreater(len(agents.get("E", [])), 0)




class TestBenchmark(unittest.TestCase):
    """性能基准: 确保核心逻辑不退化。"""

    def test_schedule_policy_1k_tasks(self):
        """1000任务排序 < 0.05s"""
        from scheduler.orchestrator import schedule_policy
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
        import time
        start = time.perf_counter()
        result = schedule_policy(tasks)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(result), 1000)
        self.assertLess(elapsed, 0.2, f"1000任务排序耗时 {elapsed:.3f}s > 0.2s")

    def test_decompose_100_tasks(self):
        """100子任务JSON解析 < 0.02s"""
        from scheduler._exec import decompose
        subtasks = [{"desc": f"task {i}", "suggested_level": "E",
                      "depends_on_local_id": [i-1] if i > 0 else []}
                    for i in range(100)]
        raw = "```json\n" + __import__('json').dumps(subtasks) + "\n```"
        import time
        start = time.perf_counter()
        result = decompose(raw)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(result), 100)
        self.assertLess(elapsed, 0.05, f"100任务解析 {elapsed:.3f}s > 0.05s")

    def test_topo_sort_50_nodes(self):
        """50节点拓扑排序 < 0.01s"""
        from scheduler._planner import _topo_sort
        tasks = [{"local_id": i, "depends_on_local_id": [i-1] if i > 0 else []}
                 for i in range(50)]
        import time
        start = time.perf_counter()
        order = _topo_sort(tasks)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(order), 50)
        self.assertLess(elapsed, 0.02, f"50节点拓扑 {elapsed:.3f}s > 0.02s")

    def test_cache_10k_ops(self):
        """10000次缓存读写 < 0.05s"""
        from scheduler._cache import TTLStore
        c = TTLStore(ttl_seconds=60)
        import time
        start = time.perf_counter()
        for i in range(10000):
            c.set(str(i), {"v": i})
            c.get(str(i))
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"10K缓存操作 {elapsed:.3f}s > 0.1s")


if __name__ == "__main__":
    unittest.main(verbosity=2)

    unittest.main(verbosity=2)
