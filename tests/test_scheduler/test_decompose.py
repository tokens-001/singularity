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
        try:
            metrics = dag_metrics()
            assert metrics["delta"] >= 2  # 至少 B→C 路径长度
        finally:
            # 清理: 用真实 create() 会写 .qidian/tasks, 不留残留
            for t in (a, b, c):
                p = tasks_dir() / f"{t.id}.json"
                if p.exists():
                    p.unlink()


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
    # 键名是 omega/delta/gamma（Dilworth 反链 / 关键路径 / 耦合密度），不是老的 total_tasks
    assert "omega" in dag and "delta" in dag, f"dag_metrics 键名变了: {sorted(dag)}"
    print(f"✅ dag_metrics self-check: nodes={dag['node_count']} omega={dag['omega']} delta={dag['delta']}")


class TestEstimateNoMoney:
    """estimate_tokens 只估 token，**不估钱**。

    2026-09-11 前它会返回 `est_cost_usd = total/1e6*0.5`（注释自认"混合均价 ~$0.5/M"）
    以及 per_task/level_breakdown 里的 cost —— 全是拍的数。这里没给子任务指定模型，
    各模型单价差几十倍，任何"均价"都是编的，所以整个删掉了。
    """

    def test_no_money_in_estimate(self):
        from singularity.scheduler._planner import estimate_tokens
        est = estimate_tokens(
            [{"desc": "写登录", "phase_hint": "any"}, {"desc": "写测试", "phase_hint": "any"}],
            "给系统加登录",
        )
        assert "est_cost_usd" not in est, "不能再有编造的预估费用"
        for t in est["per_task"]:
            assert "cost" not in t, "per_task 不该带 cost"
        for v in est["level_breakdown"].values():
            assert "cost" not in v, "level_breakdown 不该带 cost"
        # token 估算本身还得在
        assert est["total_tokens"] > 0
        assert est["task_count"] == 2
