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
                       emb=[0.1, 0.2], attrs={"status": "done", "level": "any"})
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


class TestEmbedModelActuallyLoads:
    """回归：`_get_embed_model` 里**真的 import 了** SentenceTransformer。

    2026-09-11 发现的实际 bug：那个 import **从来就不存在**，于是每次调用都抛
    NameError，被下面那句「下载失败 → 降级跳过」的 `except Exception` 一起吞掉。
    嵌入路径**一次都没生效过**，而表面症状只是"降级"：

      · 技能相关性过滤退化成"取绑定列表前 2 个"（绑 5 个只有 2 个生效，且是固定的）
      · 记忆语义直查 `find_similar()` 永远返回空

    这条测试不加载真模型（420MB / 17s），只钉住"名字解析得到" —— 正是坏掉的那一步。
    """

    def test_sentence_transformer_is_reachable(self, monkeypatch):
        import singularity.scheduler._memory_core as mc
        import sentence_transformers

        sentinel = object()
        monkeypatch.setattr(mc, "_EMBED_MODEL", None)          # 绕开懒加载缓存
        # **必须清环境变量**：`tests/test_imports.py` / `smoke_test.py` /
        # `test_exec_run.py` 在**模块导入时**就 `os.environ["QIDIAN_SKIP_EMBED"]="1"`
        # （它们是独立脚本，pytest 也会收集），于是这个开关会泄漏到别的用例 ——
        # 单跑本文件绿、全量跑红，红得莫名其妙。这条用例不该依赖环境。
        monkeypatch.delenv("QIDIAN_SKIP_EMBED", raising=False)
        seen = {}

        def _fake(name, **kw):
            seen["name"], seen["kw"] = name, kw
            return sentinel

        monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _fake)
        assert mc._get_embed_model() is sentinel
        # 2026-09-12 加：必须带 local_files_only=True。不带的话 transformers 会去
        # huggingface.co 查 metadata（**哪怕模型已在缓存里**），断网时退避重试**挂死**，
        # 而挂起不是异常、except 拦不住 —— 见防御模式 §57。
        assert seen["kw"].get("local_files_only") is True, \
            f"加载嵌入模型必须只读本地缓存，实际参数: {seen['kw']}"

    def test_并发进模型会被串起来(self, monkeypatch):
        """**MPS 并发使用 = 段错误**（2026-09-16 真机坐实，代价是整个后端进程没了）。

        症状**没有第二次机会**：崩在 C++ 里，Python 层一个字都留不下 ——
        日志停在上一行、没有 traceback、进程无声消失。macOS 崩溃报告里是
        `EXC_BAD_ACCESS / SIGSEGV` ＋
        `libtorch → at::native::mps::copy_cast_kernel_mps → to_device`。

        形状：同一个 `SentenceTransformer` 被**多个线程同时**用。而委员会就是
        **多模型并行 dispatch**、每个并行分支都来查记忆/技能 ⇒ 一起进模型。
        实测（本机）：单线程 encode 正常；**8 线程同时 encode 同一个模型 = 必崩**。

        ⚠️ 这条钉的是**接线**（`_embed` 里那把锁在不在），不是"锁实现得对不对" ——
        所以用假模型自己数并发，**不加载真模型**。
        真机复现/复验用的独立脚本见 `docs/防御模式.md` §73。

        变异验证：`_EMBED_LOCK` 换成 `contextlib.nullcontext()` → 红。
        """
        import threading
        import time as _t

        import singularity.scheduler._memory_core as mc

        class _Vec:
            def tolist(self):
                return [0.0] * 384

        class _FakeModel:
            """自己数"同时有几个线程在里面"。"""

            def __init__(self):
                self.n = 0
                self.max_n = 0
                self._l = threading.Lock()

            def encode(self, text, normalize_embeddings=True):
                with self._l:
                    self.n += 1
                    self.max_n = max(self.max_n, self.n)
                _t.sleep(0.01)          # 拉大窗口，不然并发撞不上
                with self._l:
                    self.n -= 1
                return _Vec()

        fake = _FakeModel()
        monkeypatch.setattr(mc, "_get_embed_model", lambda: fake)

        ts = [threading.Thread(target=mc._embed, args=(f"文本{i}",))
              for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        assert fake.max_n == 1, (
            f"有 {fake.max_n} 个线程同时进了 model.encode() —— "
            f"在 MPS 上这就是段错误：**真机上整个后端进程会无声消失**")

    def test_skip_env_still_short_circuits(self, monkeypatch):
        """QIDIAN_SKIP_EMBED=1 时照旧跳过（CI 用）。"""
        import singularity.scheduler._memory_core as mc
        monkeypatch.setattr(mc, "_EMBED_MODEL", None)
        monkeypatch.setenv("QIDIAN_SKIP_EMBED", "1")
        assert mc._get_embed_model() is None

    def test_load_failure_leaves_a_trail(self, monkeypatch):
        """加载真失败时必须告警 —— 之前那条静默的 except 是整件事查不出来的原因。"""
        import singularity.scheduler._memory_core as mc
        import sentence_transformers

        warns = []
        monkeypatch.setattr("singularity.scheduler.witness.warn",
                            lambda *a, **k: warns.append(a))
        monkeypatch.setattr(mc, "_EMBED_MODEL", None)
        monkeypatch.delenv("QIDIAN_SKIP_EMBED", raising=False)

        def _boom(name, **kw):
            raise RuntimeError("模拟下载失败")

        monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _boom)
        assert mc._get_embed_model() is None
        assert any("embed_model_load_failed" in str(w) for w in warns)


class TestSystem2StatusVocabulary:
    """`system2_extract` 必须认**两套**终态词汇 —— 写入方不是一个。

    `_exec.py` 写 `TaskStatus.value`（done/failed/blocked/rolled_back），
    而 `neijinglu`（trace 重建路径）写 `final_status`
    （**delivered** / delivered_unverified / blocked / rolled_back）。
    原来只认前者 → `delivered`（正常的成功终态）两个列表都不匹配
    → **成功样本被静默丢掉**。

    实测危害不止"少一条洞察"：拿存量 19 条 trace 的真实分布
    （5 delivered / 10 blocked / 4 delivered_unverified）喂进去，
    报出 `failure_hotspot`、success_rate 0.0 —— **真实是 9 成 10 败 = 47%**。
    结论是**反的**。
    """

    def _extract(self, tmp_path, monkeypatch, statuses):
        import singularity.scheduler._memory_lifecycle as ml
        from singularity.scheduler._memory_core import EventNode
        from singularity.scheduler import config
        # 只改 config.QIDIAN_DIR 就够 —— 内存模块的路径 2026-09-11 起是**读时现算**的
        # （`ml._insights_path()` 等）。以前是模块级常量，那时必须在这里逐个补刀，
        # 漏一个测试就写进**真实**的 `.qidian/memory/insights.json`（我这轮写脏过一次）。
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
        nodes = {}
        for i, st in enumerate(statuses):
            nodes[f"t{i}"] = EventNode(task_id=f"t{i}", content="x", timestamp=float(i),
                                       attrs={"status": st, "route_type": "default",
                                              "route_level": "any"})
        monkeypatch.setattr(ml, "_load_events", lambda: nodes)
        return ml.system2_extract()

    def test_delivered_counts_as_success(self, tmp_path, monkeypatch):
        """9 成 10 败 = 47% → 正常区间，不该报任何洞察。

        修之前 `delivered` 被丢掉 → 10 败 0 成 → 报 failure_hotspot（结论是反的）。
        """
        r = self._extract(tmp_path, monkeypatch,
                          ["delivered"] * 5 + ["blocked"] * 10 + ["delivered_unverified"] * 4)
        assert r.get("insights") == [], f"不该报洞察，实际 {r.get('insights')}"

    def test_delivered_dominant_reports_success(self, tmp_path, monkeypatch):
        """全是 delivered 且够多 → 应该报 high_success_pattern（不是什么都不报）。"""
        r = self._extract(tmp_path, monkeypatch, ["delivered"] * 10 + ["blocked"] * 1)
        types = [i["type"] for i in r.get("insights", [])]
        assert types == ["high_success_pattern"], types

    def test_exec_taskstatus_values_still_work(self, tmp_path, monkeypatch):
        """另一套词汇（`_exec.py` 写的）不能因为这次改动失效。"""
        r = self._extract(tmp_path, monkeypatch, ["done"] * 10 + ["failed"] * 1)
        types = [i["type"] for i in r.get("insights", [])]
        assert types == ["high_success_pattern"], types
