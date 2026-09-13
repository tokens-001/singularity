"""`query(max_depth=1)` 的返回值必须**和深度 ≥2 同形状** —— 否则消费端读不到。

出处：外派 扫bug-02 ⑦ / G 的 `核待验证`（我回当前树核过，并且核出了它说的**不准确之处**）：
它说"整条记忆信号链是死代码"，实际是**只有最浅的那条路死了** ——

  `pre_search.py:131-137` 读的是 `traversal["narrative" | "intent" | "graph_coverage"]`，
  那是 `synthesize()` 的返回形状，**深度 2/3 正好就是它**；
  而深度 1 的分支给的是 `{summary, nodes, synthesis_model}` —— 三个键一个都读不到。

后果（都在 `deep=False` 这条最常见路上，即 `retry_count == 0` 的首次尝试）：
  · `mem.narrative` 恒空 ⇒ `apply_escalation` 里"高分记忆 → routing hint"永不触发
  · `mem.intent` 恒为默认的 `"semantic"` ⇒ trace 文案失真
  · 刚算出来的 `summary` / `nodes` **没有任何消费者**，白算

这里测的是**接线**：上游数据源（`find_similar`）打桩，看 `query()` 交给消费端的形状对不对。
（打桩上游是正当的 —— 被测的是 `query` 的形状映射，不是 `find_similar` 本身。）
"""
import pytest

from singularity.scheduler import config
from singularity.scheduler import _memory_graph as G


_HITS = [
    {"task_id": "t-aaa", "description": "改过 pre_search 的早退", "similarity": 0.83,
     "timestamp": 100, "trajectory": "", "tool_seq": [], "abstraction": None},
    {"task_id": "t-bbb", "description": "另一个任务", "similarity": 0.41,
     "timestamp": 99, "trajectory": "", "tool_seq": [], "abstraction": None},
]


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(G, "find_similar", lambda desc, top_k=5: list(_HITS))
    return _HITS


def test_深度1也要吐消费端读的那三个键(seeded):
    """形状契约：`narrative` / `intent` / `graph_coverage` 一个都不能少。

    少一个 = `pre_search` 那边 `.get(默认值)` 兜住、**不报错**，
    于是信号静默消失（这正是它藏了这么久的原因）。
    """
    trav = G.query("为什么任务会重跑", max_depth=1)["traversal"]
    for key in ("narrative", "intent", "graph_coverage"):
        assert key in trav, f"深度 1 的 traversal 缺 `{key}` —— 消费端读不到，且不会报错"


def test_语义命中要带_score_否则消费端过滤全灭(seeded):
    """`pre_search.py:225` 按 `r.get("score", 0) >= 0.1` 过滤 —— 键名不对，信号就死在键名上。

    语义命中给的字段叫 `similarity`，消费端要 `score`。
    """
    trav = G.query("改 pre_search", max_depth=1)["traversal"]
    narr = trav["narrative"]
    assert narr, "语义有命中，narrative 却是空的"

    # 复刻消费端那行过滤（pre_search.py:225）——它必须挑得出来
    high = [r for r in narr if r.get("score", 0) >= 0.1]
    assert high, f"按消费端的判据挑不出任何一条 ⇒ routing hint 永不触发：{narr}"
    assert high[0]["task_id"] in ("t-aaa", "t-bbb")
    assert abs(high[0]["score"] - 0.83) < 1e-6, f"score 没跟 similarity 对上：{high[0]}"


def test_intent_要真算_不是默认值(seeded):
    """`intent` 原来恒为默认的 `"semantic"`。用一条明确带因果词的查询验它真算了。"""
    trav = G.query("为什么会这样，什么原因导致的", max_depth=1)["traversal"]
    assert trav["intent"] != "", "intent 是空的"
    # `detect_intent` 的因果档 —— 只断言"不是默认值"，具体词表不是本测试要钉的
    assert trav["intent"] == G.detect_intent("为什么会这样，什么原因导致的"), \
        f"intent 没从句子里算，落回默认值了：{trav['intent']}"


def test_深度1没有图遍历_覆盖就该是空的(seeded):
    """深度 1 **不走** Beam Search ⇒ `graph_coverage` 空是**诚实**的。

    这里钉住它：别为了"看着有信号"而编一个非空的覆盖率出来
    （`pre_search` 那边 `if mem.graph_coverage:` 会照着它报一条覆盖信号）。
    """
    trav = G.query("随便什么", max_depth=1)["traversal"]
    assert trav["graph_coverage"] == {}


def test_没有命中也别崩(tmp_path, monkeypatch):
    """对照：语义一条都没命中时，形状仍要齐、`narrative` 空列表。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(G, "find_similar", lambda desc, top_k=5: [])
    trav = G.query("空库", max_depth=1)["traversal"]
    assert trav["narrative"] == []
    assert trav["graph_coverage"] == {}
    assert "intent" in trav


def test_深度1的条目要受_mem_type_过滤(tmp_path, monkeypatch):
    """顺序陷阱：条目必须在 `mem_type` 过滤**之后**建。

    反过来的话，深度 1 的 narrative 会绕过类型过滤，把别的类型的记忆也报出去。
    """
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(G, "find_similar", lambda desc, top_k=5: list(_HITS))
    # 只让 t-bbb 是 bug_fix 类
    class _N:
        def __init__(self, t): self.attrs = {"mem_type": t}
    monkeypatch.setattr(G, "_load_events",
                        lambda: {"t-aaa": _N("architecture"), "t-bbb": _N("bug_fix")})

    trav = G.query("改 pre_search", max_depth=1, mem_type="bug_fix")["traversal"]
    ids = [r["task_id"] for r in trav["narrative"]]
    assert ids == ["t-bbb"], f"没按 mem_type 过滤（或过滤发生得太晚）：{ids}"
