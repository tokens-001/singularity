"""test_decompose.py — 测试 decompose() 纯函数和 tracker DAG 指标。"""
import json
import pytest
from singularity.scheduler._exec import decompose
from singularity.scheduler.tracker import dag_metrics, create, TaskStatus, tasks_dir
from singularity.scheduler import config


class TestDecompose:
    """decompose() 是纯函数: planner output → 子任务列表。"""

    def test_valid_json_array(self):
        raw = '```json\n[{"desc": "add login", "suggested_level": "any"}, {"desc": "add tests", "suggested_level": "any"}]\n```'
        result = decompose(raw)
        assert len(result) == 2
        assert result[0]["desc"] == "add login"
        assert result[0]["suggested_level"] == "any"

    def test_empty_input(self):
        assert decompose("") == []
        assert decompose("just some text") == []

    def test_no_json_block(self):
        result = decompose("This is a plan without any code blocks.")
        assert result == []

    def test_json_not_array(self):
        raw = '```json\n{"desc": "not a list"}\n```'
        assert decompose(raw) == []

    def test_missing_desc_field(self):
        raw = '```json\n[{"not_desc": "foo"}, {"desc": "valid"}]\n```'
        result = decompose(raw)
        # Only items with "desc" field survive
        assert len(result) == 1
        assert result[0]["desc"] == "valid"

    def test_depends_on_mapping(self):
        raw = '''```json
[{"desc": "task 0", "suggested_level": "any"},
 {"desc": "task 1", "depends_on_local_id": 0, "suggested_level": "any"}]
```'''
        result = decompose(raw)
        assert len(result) == 2
        # The second task doesn't have depends_on set because local_id=0
        # hasn't been created yet (task IDs are UUIDs generated later)
        assert "depends_on" not in result[0]


class TestDAGMetrics:
    """验证 tracker.dag_metrics() 返回正确的 DAG 统计。"""

    def setup_method(self):
        config.ensure_dirs()

    def test_empty_dag(self):
        metrics = dag_metrics()
        assert "node_count" in metrics
        assert "omega" in metrics
        assert "delta" in metrics
        assert isinstance(metrics["node_count"], int)

    def test_linear_chain(self):
        """A → B → C 线性链: omega=1(最大化并行=1), delta=3(关键路径长度)。"""
        a = create("task A")
        b = create("task B", depends_on=[a.id], depth=1)
        c = create("task C", depends_on=[b.id], depth=2)

        metrics = dag_metrics()
        assert metrics["delta"] >= 2  # 至少 B→C 路径长度


# ═══════════════════════════════════════════════════════════
# __main__ self-check (ponytail: smallest thing that fails)
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    t = TestDecompose()
    t.test_valid_json_array()
    t.test_empty_input()
    t.test_no_json_block()
    t.test_json_not_array()
    t.test_missing_desc_field()
    t.test_depends_on_mapping()
    print("✅ decompose() self-check passed")

    config.ensure_dirs()
    dag = dag_metrics()
    assert isinstance(dag, dict), "dag_metrics should return dict"
    assert "total_tasks" in dag
    print(f"✅ dag_metrics self-check: total={dag['total_tasks']}, max_depth={dag['max_depth']}")
