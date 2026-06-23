"""MAGMA memory tests — eviction, event node, RRF."""
from singularity.scheduler.memory import EventNode, _calculate_importance, _evict_if_needed, _rrf_anchors


class TestMAGMAMemoryEviction:
    """T12: MAGMA 记忆 LRU 驱逐 + 重要性评分。"""

    def test_importance_scoring(self):
        now = 1_000_000.0
        good = EventNode("t1", "done task", now - 100, [], {"status": "done"})
        bad = EventNode("t2", "failed task", now - 86_400, [], {"status": "failed"})
        events = {"t1": good, "t2": bad}
        edges = {"causal": [("t1", "t2")]}
        s1 = _calculate_importance("t1", good, events, edges, now)
        s2 = _calculate_importance("t2", bad, events, edges, now)
        assert s1 > s2, f"success+referenced should outrank failed: {s1:.3f} vs {s2:.3f}"

    def test_eviction_below_cap(self):
        events = {}
        for i in range(10):
            events[str(i)] = EventNode(str(i), f"task {i}", 1_000_000.0, [], {})
        evicted = _evict_if_needed(events, {}, max_events=20)
        assert evicted == 0
        assert len(events) == 10

    def test_eviction_above_cap(self):
        now = 1_000_000.0
        events = {}
        for i in range(15):
            status = "done" if i < 10 else "failed"
            events[str(i)] = EventNode(str(i), f"task {i}", now, [], {"status": status})
        evicted = _evict_if_needed(events, {}, max_events=10)
        assert evicted == 5
        assert len(events) == 10
        for node in events.values():
            assert node.attrs.get("status") == "done"

    def test_edge_cleanup_on_eviction(self):
        now = 1_000_000.0
        events = {
            "keep": EventNode("keep", "important", now, [], {"status": "done"}),
            "drop": EventNode("drop", "junk", now - 86_400_000, [], {"status": "failed"}),
        }
        edges = {"causal": [("drop", "keep")], "semantic": [("drop", "keep", 0.7)]}
        evicted = _evict_if_needed(events, edges, max_events=1)
        assert evicted == 1
        assert "drop" not in events
        assert "keep" in events
        assert len(edges["causal"]) == 0
        assert len(edges["semantic"]) == 0


class TestPropertyEventNode:
    """Memory EventNode 不变量。"""

    def test_embedding_dimension_consistency(self):
        n1 = EventNode(task_id="t1", content="desc", timestamp=1.0, emb=[0.1] * 128)
        n2 = EventNode(task_id="t2", content="desc2", timestamp=2.0, emb=[0.2] * 128)
        assert len(n1.emb) == len(n2.emb)

    def test_to_dict_roundtrip(self):
        n1 = EventNode(task_id="t1", content="test desc", timestamp=100.0,
                       emb=[0.1, 0.2], attrs={"status": "done", "level": "D"})
        d = n1.to_dict()
        n2 = EventNode.from_dict(d)
        assert n1.task_id == n2.task_id
        assert n1.content == n2.content
        assert n1.attrs.get("status") == n2.attrs.get("status")

    def test_default_attrs(self):
        n = EventNode(task_id="t1", content="desc", timestamp=1.0)
        assert n.attrs == {}


class TestPropertyRRF:
    """RRF 融合不变量。"""

    def test_rrf_anchors_empty_events(self):
        result = _rrf_anchors(query_tokens=[0.1, 0.2], query_text="test query",
                              events={}, edges={}, k=5)
        assert isinstance(result, list)

    def test_rrf_k_respected(self):
        events = {
            f"t{i}": EventNode(task_id=f"t{i}", content=f"desc{i}", timestamp=float(i))
            for i in range(20)
        }
        result = _rrf_anchors(query_tokens=[0.1] * 128, query_text="test",
                              events=events, edges={}, k=5)
        assert len(result) <= 5
