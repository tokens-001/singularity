"""Decompose + TopoSort tests."""
from singularity.scheduler._exec import decompose
from singularity.scheduler._planner import _topo_sort


class TestDecompose:
    """Planner JSON 解析。"""

    def test_valid_decomposition(self):
        raw = '```json\n[{"desc": "task A", "suggested_level": "any", "depends_on_local_id": []}]\n```'
        tasks = decompose(raw)
        assert len(tasks) == 1
        assert tasks[0]["desc"] == "task A"

    def test_no_json_block(self):
        tasks = decompose("just some text, no json")
        assert tasks == []

    def test_multiple_tasks_with_deps(self):
        raw = """```json
[
  {"desc": "task 1", "suggested_level": "any", "depends_on_local_id": []},
  {"desc": "task 2", "suggested_level": "any", "depends_on_local_id": [0]}
]
```"""
        tasks = decompose(raw)
        assert len(tasks) == 2
        assert tasks[1]["depends_on_local_id"] == [0]


class TestTopoSort:
    """Kahn 拓扑排序。"""

    def test_linear_deps(self):
        tasks = [
            {"local_id": 0, "depends_on_local_id": []},
            {"local_id": 1, "depends_on_local_id": [0]},
            {"local_id": 2, "depends_on_local_id": [1]},
        ]
        assert _topo_sort(tasks) == [0, 1, 2]

    def test_no_deps(self):
        tasks = [{"local_id": 0}, {"local_id": 1}, {"local_id": 2}]
        order = _topo_sort(tasks)
        assert order is not None
        assert set(order) == {0, 1, 2}

    def test_cycle_detection(self):
        tasks = [
            {"local_id": 0, "depends_on_local_id": [1]},
            {"local_id": 1, "depends_on_local_id": [0]},
        ]
        assert _topo_sort(tasks) is None
