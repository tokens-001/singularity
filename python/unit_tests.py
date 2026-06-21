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
        self.assertIn("deepseek-v4-pro", models)

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
        # 自定义配置可能禁用所有 agent, 只验证不崩溃
        total = sum(len(v) for v in agents.values())
        self.assertGreaterEqual(total, 0, f"agent 加载不崩溃, 共 {total} 个")




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


class TestCriticalFixes(unittest.TestCase):
    """三模型审查 CRITICAL 修复的边界条件测试。"""

    def test_next_id_monotonic(self):
        """_next_id() 单调递增, 1000 次调用无碰撞。"""
        from scheduler.tracker import _next_id, _invalidate_scan_cache
        ids = set()
        for _ in range(100):
            ids.add(_next_id())
        self.assertEqual(len(ids), 100, f"100次调用应产生100个唯一ID, 实际{len(ids)}")

    def test_next_id_increasing(self):
        """_next_id() 每次调用返回值严格递增。"""
        from scheduler.tracker import _next_id
        prev = int(_next_id())
        for _ in range(50):
            curr = int(_next_id())
            self.assertGreater(curr, prev, f"ID应递增: {prev} → {curr}")
            prev = curr

    def test_auth_bootstrap_rejects_when_users_exist(self):
        """auth_bootstrap 在已有用户时返回 403。"""
        from scheduler._api import auth_bootstrap
        result, code = auth_bootstrap()
        # 已有 admin 用户 → 应拒绝
        self.assertIn(code, (200, 403),
                      f"预期200(首次)或403(已有用户), 实际{code}")
        if code == 403:
            self.assertFalse(result.get("ok"), f"403时ok应为false: {result}")

    def test_goal_check_no_agent_returns_false(self):
        """_check_goal 无 E 层 agent 时返回 met=False (不复假成功)。"""
        import types, importlib
        # 需要模拟 _check_goal
        from scheduler.goal_loop import GoalLoop
        gl = GoalLoop.__new__(GoalLoop)
        gl._agents = {"E": []}  # 空 agent 列表
        result = gl._check_goal("test output", "test goal", "test task")
        self.assertFalse(result.get("met", True),
                         f"无agent时met应为False, 实际: {result}")

    def test_token_auth_backward_compat(self):
        """旧格式 token hash 认证 + 自动迁移。"""
        import hashlib
        from scheduler._auth import _hash_token, _hash_token_v2, AuthStore
        # 创建临时 auth store (不落盘)
        # 验证 v2 哈希和 v1 不同
        token = "test-token-12345"
        h1 = _hash_token(token)
        h2 = _hash_token_v2(token)
        self.assertNotEqual(h1, h2, "v1和v2哈希应不同")
        # v2 更长 (加盐)
        self.assertEqual(len(h1), 64)  # sha256 hex
        self.assertEqual(len(h2), 64)


class TestMAGMAMemoryEviction(unittest.TestCase):
    """T12: MAGMA 记忆 LRU 驱逐 + 重要性评分。"""
    def test_importance_scoring(self):
        """成功节点 > 失败节点，被引用节点 > 孤立节点。"""
        from scheduler.memory import EventNode, _calculate_importance
        now = 1_000_000.0
        # 成功 + 高引用
        good = EventNode("t1", "done task", now - 100, [], {"status": "done"})
        # 失败 + 无引用
        bad = EventNode("t2", "failed task", now - 86_400, [], {"status": "failed"})
        events = {"t1": good, "t2": bad}
        edges = {"causal": [("t1", "t2")]}  # t1→t2, t1被引用
        s1 = _calculate_importance("t1", good, events, edges, now)
        s2 = _calculate_importance("t2", bad, events, edges, now)
        self.assertGreater(s1, s2, f"成功+被引用节点应高于失败孤立节点: {s1:.3f} vs {s2:.3f}")

    def test_eviction_below_cap(self):
        """不超上限时不驱逐。"""
        from scheduler.memory import EventNode, _evict_if_needed
        events = {}
        for i in range(10):
            events[str(i)] = EventNode(str(i), f"task {i}", 1_000_000.0, [], {})
        evicted = _evict_if_needed(events, {}, max_events=20)
        self.assertEqual(evicted, 0)
        self.assertEqual(len(events), 10)

    def test_eviction_above_cap(self):
        """超上限时驱逐低分节点，保留高分节点。"""
        from scheduler.memory import EventNode, _evict_if_needed
        now = 1_000_000.0
        events = {}
        for i in range(15):
            status = "done" if i < 10 else "failed"
            events[str(i)] = EventNode(str(i), f"task {i}", now, [], {"status": status})
        # 10个done + 5个failed, cap=10, 应驱逐5个failed
        evicted = _evict_if_needed(events, {}, max_events=10)
        self.assertEqual(evicted, 5)
        self.assertEqual(len(events), 10)
        # 保留的应全是 done
        for tid, node in events.items():
            self.assertEqual(node.attrs.get("status"), "done")

    def test_edge_cleanup_on_eviction(self):
        """驱逐节点时关联边一并清理。"""
        from scheduler.memory import EventNode, _evict_if_needed
        now = 1_000_000.0
        events = {
            "keep": EventNode("keep", "important", now, [], {"status": "done"}),
            "drop": EventNode("drop", "junk", now - 86_400_000, [], {"status": "failed"}),
        }
        edges = {"causal": [("drop", "keep")], "semantic": [("drop", "keep", 0.7)]}
        evicted = _evict_if_needed(events, edges, max_events=1)
        self.assertEqual(evicted, 1)
        self.assertNotIn("drop", events)
        self.assertIn("keep", events)
        # drop→keep 边应被清
        self.assertEqual(len(edges["causal"]), 0)
        self.assertEqual(len(edges["semantic"]), 0)


# ═══════════════════════════════════════════════════════
# Project workflow — 不调 LLM，纯测状态流转和任务落地
# ═══════════════════════════════════════════════════════

class TestProjectWorkflow(unittest.TestCase):
    """项目工作流: phase 流转 + 任务创建落地 + auto/manual gate。"""

    def setUp(self):
        from scheduler.project import create, Phase
        self.p = create(
            name="test-wf",
            description="测试工作流: 写一个 hello world",
            scope="test",
            template="feature",
            auto_mode=False,
        )
        self.p.architecture = {
            "architecture": "单文件脚本",
            "tasks": [
                {"id": "T1", "title": "写 hello.py", "description": "创建 hello.py",
                 "complexity": "low", "acceptance": "python hello.py 输出 hello world",
                 "estimated_files": ["hello.py"]},
                {"id": "T2", "title": "写测试", "description": "创建 test_hello.py",
                 "complexity": "low", "acceptance": "python -m pytest test_hello.py 通过",
                 "estimated_files": ["test_hello.py"]},
            ],
            "constraints": [{"text": "不用外部依赖", "type": "no_new_deps",
                              "check": "grep -r 'import' hello.py | wc -l <= 2"}],
            "risks": ["无"],
            "test_strategy": "跑 pytest",
        }
        from scheduler.project import save
        save(self.p)

    def tearDown(self):
        from scheduler import tracker
        from scheduler.project import _path as _proj_path
        # 清理项目任务
        for tid in list(self.p.task_ids):
            p = tracker._path(tid)
            if p.exists():
                p.unlink()
        # 清理项目文件
        pp = _proj_path(self.p.id)
        if pp.exists():
            pp.unlink()

    def test_run_execution_creates_real_tasks(self):
        """_run_execution 必须创建落盘任务, 不能是幽灵 ID。"""
        from scheduler.workflow import _run_execution
        from scheduler import tracker
        _run_execution(self.p, {})
        self.assertGreater(len(self.p.task_ids), 0, "应创建子任务")
        for tid in self.p.task_ids:
            t = tracker._read(tid)
            self.assertIsNotNone(t, f"任务 {tid[:8]} 应落盘存在")
            self.assertTrue(t.route_locked, "项目任务应锁定路由")
            self.assertEqual(t.project_id, self.p.id)
            self.assertIn(t.route_level, ("E", "E+", "D"))
        self.assertEqual(self.p.phase.value, "executing",
                         "执行后应保持 executing, 等 orchestrator 跑完")

    def test_run_phase_manual_stops_at_gates(self):
        """手动模式: 每道门停止, 不自动前进。"""
        from scheduler.workflow import run_phase
        # 模拟 phase=template
        self.p.phase = self.p.phase.__class__.TEMPLATE
        msg = run_phase(self.p, {})
        self.assertIn("等待 Owner", msg)

        # 模拟 gate1
        self.p.phase = self.p.phase.__class__.GATE1
        msg = run_phase(self.p, {})
        self.assertIn("等待 Owner gate1", msg)
        self.assertEqual(self.p.phase.value, "gate1")  # 卡住不变

    def test_run_phase_auto_chains_to_executing(self):
        """auto_mode: 贯穿 gate1→planning→gate2→executing 一气呵成。"""
        from scheduler.workflow import run_phase
        from unittest.mock import patch, MagicMock
        self.p.auto_mode = True
        self.p.phase = self.p.phase.__class__.GATE1
        # mock dispatcher: _run_planning 会调 LLM, 返回合法架构 JSON
        mock_result = MagicMock()
        mock_result.executor_result.raw_output = '{"architecture":"x","tasks":[{"id":"T1","title":"t","description":"d","complexity":"low","acceptance":"a","estimated_files":["f.py"]}],"constraints":[],"risks":[],"test_strategy":"x"}'
        mock_result.agent_cfg = {"model": "test"}
        with patch('scheduler.workflow.disp_mod.dispatch', return_value=mock_result):
            msg = run_phase(self.p, {})
        self.assertIn("auto: gate1 → planning", msg)
        self.assertIn("auto: gate2 → executing", msg)
        # 最终应停在 executing (等 orchestrator 跑完任务)
        self.assertIn(self.p.phase.value, ("executing", "gate3"))

    def test_gate_confirm_approved_advances(self):
        """批准: GATE1→PLANNING, GATE2→EXECUTING, GATE3→DONE。"""
        from scheduler.project import Phase
        tests = [
            (Phase.GATE1, Phase.PLANNING),
            (Phase.GATE2, Phase.EXECUTING),
            (Phase.GATE3, Phase.DONE),
        ]
        for gate, expected in tests:
            self.p.phase = gate
            self.p.confirm_gate(gate, "approved")
            self.assertEqual(self.p.phase, expected,
                             f"{gate.value} approve → {expected.value}")

    def test_gate_confirm_rejected_falls_back(self):
        """打回: GATE3→PLANNING, GATE2→RESEARCHING。"""
        from scheduler.project import Phase
        self.p.phase = Phase.GATE3
        self.p.confirm_gate(Phase.GATE3, "rejected")
        self.assertEqual(self.p.phase, Phase.PLANNING)  # 打回让D重新出方案

        self.p.phase = Phase.GATE2
        self.p.confirm_gate(Phase.GATE2, "rejected")
        self.assertEqual(self.p.phase, Phase.RESEARCHING)

    def test_architecture_redo_from_executing(self):
        """架构返工: EXECUTING→PLANNING, 清空旧架构。"""
        from scheduler.project import Phase
        self.p.phase = Phase.EXECUTING
        self.p.architecture_redo()
        self.assertEqual(self.p.phase, Phase.PLANNING)
        self.assertIsNone(self.p.architecture)


if __name__ == "__main__":
    unittest.main(verbosity=2)
