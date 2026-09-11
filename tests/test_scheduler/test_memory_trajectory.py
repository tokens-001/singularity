"""记忆里的「实际产出」(trajectory)。

背景：以前 `index_task` 只存 task.description（写盘时还截到 200 字），
`agent_output`（"上次到底怎么做的"）从来没进过记忆。
对应 docs/经验分层-STAIR借鉴-20260912.md。

三条要钉住的：
  1. 存得进、取得出（往返）
  2. **不传不等于清空** —— 重复 index 不能把已存的抹成空
  3. 「全文」只在 depth>=3 展开，且只展开前几条（默认路径行为不变）
"""
import pytest

from singularity.scheduler import memory as mem
from singularity.scheduler import _memory_core as mc
from singularity.scheduler import _memory_graph as mg


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    """别真加载 sentence-transformers —— 慢，而且今天正是它挂死过（防御模式 §57）。"""
    monkeypatch.setattr(mc, "_embed", lambda text: [1.0, 0.0, 0.0])


class TestRoundTrip:
    def test_stored_and_read_back(self):
        mem.index_task(task_id="t1", description="统计文本字数",
                       trajectory="这次用 bytes.count(b'\\n') 数行")
        assert mc._load_events()["t1"].trajectory == "这次用 bytes.count(b'\\n') 数行"

    def test_missing_trajectory_does_not_wipe_existing(self):
        """反证：不带 trajectory 的重复 index 必须保留原有的，不能抹成空。"""
        mem.index_task(task_id="t1", description="任务甲", trajectory="原来怎么做的")
        mem.index_task(task_id="t1", description="任务甲", trajectory="")
        assert mc._load_events()["t1"].trajectory == "原来怎么做的"

    def test_old_record_without_key_loads(self):
        """老数据没有 trajectory 键 → 空串，不炸。"""
        node = mc.EventNode.from_dict({"task_id": "x", "content": "c", "timestamp": 1.0})
        assert node.trajectory == ""

    def test_truncated_at_storage_cap(self):
        """超过 TRAJECTORY_MAX 才截断，且截断值就是上限。"""
        long = "x" * (mc.TRAJECTORY_MAX + 500)
        node = mc.EventNode(task_id="t", content="d", timestamp=0.0, emb=[], attrs={},
                            trajectory=long)
        assert len(node.to_dict()["trajectory"]) == mc.TRAJECTORY_MAX


class TestSynthesizeExpansion:
    def _results(self, n=5):
        return [
            {"task_id": f"t{i}", "description": f"任务{i}", "score": 1.0 - i * 0.1,
             "trajectory": f"产出{i}" * 50, "timestamp": 0}
            for i in range(n)
        ]

    def test_default_does_not_expand(self):
        """默认（depth 1/2）行为不变：没有 full_text。"""
        out = mg.synthesize(self._results(), "任务")
        assert all("full_text" not in it for it in out["narrative"])

    def test_include_full_expands_only_top_n(self):
        out = mg.synthesize(self._results(), "任务", include_full=True)
        nar = out["narrative"]
        assert nar[0]["full_text"].startswith("产出0")
        assert "full_text" in nar[1]
        assert "full_text" not in nar[2], "只该展开前 EXPAND_TOP 条"

    def test_trajectory_not_leaked_outside(self):
        """候选材料不该跟着结果往外传（没展开的尤其）。"""
        out = mg.synthesize(self._results(), "任务", include_full=True)
        assert all("trajectory" not in it for it in out["narrative"])

    def test_truncation_is_flagged(self):
        results = [{"task_id": "a", "description": "d", "score": 1.0,
                    "trajectory": "x" * (mg.EXPAND_CHARS + 50), "timestamp": 0}]
        out = mg.synthesize(results, "d", include_full=True)
        assert out["narrative"][0]["full_text_truncated"] is True

    def test_no_trajectory_no_full_text(self):
        """有历史命中但没采到产出 → 不塞空串。"""
        results = [{"task_id": "a", "description": "d", "score": 1.0,
                    "trajectory": "", "timestamp": 0}]
        out = mg.synthesize(results, "d", include_full=True)
        assert "full_text" not in out["narrative"][0]


# 阶段条目的描述 = 同一条项目描述 + 阶段前缀，互相的 Jaccard 很高
_DESC = " ".join(["统计", "文本", "行数", "单词数", "字符数", "支持", "json", "输出", "单文件", "实现"])


class TestStageTagAndDedup:
    def test_stage_recorded_in_attrs(self):
        mem.index_task(task_id="research_p1", description=f"[researching] {_DESC}",
                       stage="researching", trajectory="调研产出全文", force=True)
        assert mc._load_events()["research_p1"].attrs.get("stage") == "researching"

    def test_force_keeps_both_phases(self):
        """反证：两个阶段只差一个前缀，不去重的话第二个会被静默挤掉。"""
        mem.index_task(task_id="research_p1", description=f"[researching] {_DESC}",
                       stage="researching", trajectory="调研产出", force=True)
        mem.index_task(task_id="architect_p1", description=f"[planning] {_DESC}",
                       stage="planning", trajectory="架构产出", force=True)
        ev = mc._load_events()
        assert "research_p1" in ev and "architect_p1" in ev

    def test_without_force_the_second_is_dropped(self):
        """对照组 —— 证明上面那条 force 不是摆设（去掉 force 这条就会红）。"""
        mem.index_task(task_id="r1", description=f"[researching] {_DESC}", force=True)
        mem.index_task(task_id="r2", description=f"[planning] {_DESC}")  # 不给 force
        ev = mc._load_events()
        assert "r1" in ev
        assert "r2" not in ev, "描述近乎相同且未 force → 应被去重跳过"
