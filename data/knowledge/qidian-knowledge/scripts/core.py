#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核⼼检索引擎
=============
- BM25 排序（零依赖，自实现）
- 域路由（按文档类型分域）
- 查询自动判域（关键词命中 → 域）
- 基于 JSON 索引运行（不每次重扫 markdown）
"""

import json
import re
from pathlib import Path
from math import log
from collections import defaultdict

# ============ 引用分词器 ============
import sys

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # embedder → config
from tokenizer import ChineseTokenizer

# ============ CONFIGURATION ============
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "data"
INDEX_PATH = DATA_DIR / "index" / "corpus.json"
SYNONYMS_PATH = DATA_DIR / "synonyms.csv"
MAX_RESULTS = 5

# 文档类型 → 索引中 type 字段映射
DOMAIN_CONFIG = {
    "principle": {
        "label": "原则 (P)",
        "search_on": ["title", "positioning", "sections_text"],
        "role": "候选必须满足的原则",
    },
    "decision": {
        "label": "决策 (D)",
        "search_on": ["title", "positioning", "sections_text"],
        "role": "已做决策，查方向一致性（词法级提示，非语义矛盾检测）",
    },
    "insight": {
        "label": "洞察 (I)",
        "search_on": ["title", "positioning", "sections_text"],
        "role": "方法论/复盘，软参考",
    },
    "case": {
        "label": "案卷 (knowledge)",
        "search_on": ["title", "positioning", "tags_text", "sections_text"],
        "role": "前例、概念定义",
    },
    "question": {
        "label": "未决问题 (Q)",
        "search_on": ["title", "sections_text"],
        "role": "待验证，提示不确定",
    },
    "experiment": {
        "label": "实验 (E)",
        "search_on": ["title", "sections_text"],
        "role": "实验状态",
    },
    "reference": {
        "label": "参考",
        "search_on": ["title", "sections_text"],
        "role": "参考资料",
    },
}

# 查询意图 → 目标域（用于 detect_domain）
INTENT_KEYWORDS = {
    "principle": [
        "原则",
        "骨架",
        "验证层",
        "反馈",
        "选择题",
        "架构",
        "三层",
        "技能模块",
        "协作模式",
        "双ai",
        "认知同步",
        "反例",
        "适用范围",
        "产品骨架",
        "工作流",
    ],
    "decision": [
        "决策",
        "决定",
        "选",
        "方向",
        "校准",
        "定位",
        "停止",
        "阶段",
        "执行风险",
        "d00",
    ],
    "insight": [
        "洞察",
        "认知债",
        "复盘",
        "拆解",
        "审计",
        "方法",
        "项目不是资产",
        "冻结容器",
        "编排者",
        "天工",
        "workbuddy",
        "情绪",
        "追踪",
    ],
    "case": [
        "案卷",
        "委身",
        "完美防御",
        "分级",
        "暴露",
        "能量",
        "关系",
        "意识",
        "认知地基",
        "正面指标",
        "身体维度",
        "爱情",
        "信任",
        "命理",
        "输出",
        "智能体定制",
        "学习",
        "sdf",
        "00",
    ],
    "question": [
        "问题",
        "未决",
        "收费",
        "数据安全",
        "第二个项目",
        "选什么",
        "隐私",
        "design",
        "copilot",
        "q0",
    ],
    "experiment": ["实验", "反馈机制", "法条校验", "双ai协作", "技能模块化", "e00"],
}


# ============ BM25 实现 ============
class BM25:
    def __init__(self, k1=1.5, b=1.5):
        self.k1 = k1
        self.b = b
        self.corpus = []
        self.doc_ids = []
        self.doc_lengths = []
        self.avgdl = 0
        self.idf = {}
        self.doc_freqs = defaultdict(int)
        self.N = 0

    def fit(self, documents: list):
        """documents: [(doc_id, token_list), ...]"""
        self.doc_ids = [d[0] for d in documents]
        self.corpus = [d[1] for d in documents]
        self.N = len(self.corpus)
        if self.N == 0:
            return
        self.doc_lengths = [len(doc) for doc in self.corpus]
        self.avgdl = sum(self.doc_lengths) / self.N

        for doc in self.corpus:
            seen = set()
            for word in doc:
                if word not in seen:
                    self.doc_freqs[word] += 1
                    seen.add(word)

        for word, freq in self.doc_freqs.items():
            self.idf[word] = log((self.N - freq + 1.5) / (freq + 0.5) + 1)

    def score(self, query_tokens: list) -> list:
        """返回 [(doc_id, score), ...] 按分数降序"""
        scores = []
        for idx, doc in enumerate(self.corpus):
            score = 0
            doc_len = self.doc_lengths[idx]
            term_freqs = defaultdict(int)
            for word in doc:
                term_freqs[word] += 1

            for token in query_tokens:
                if token in self.idf:
                    tf = term_freqs[token]
                    idf = self.idf[token]
                    numerator = tf * (self.k1 + 1)
                    denominator = tf + self.k1 * (
                        1 - self.b + self.b * doc_len / self.avgdl
                    )
                    score += idf * numerator / max(denominator, 1e-9)

            scores.append((self.doc_ids[idx], score))

        return sorted(scores, key=lambda x: x[1], reverse=True)


# ============ 检索引擎 ============
class KnowledgeSearchEngine:
    def __init__(self, index_path: Path = None, synonyms_path: Path = None):
        if index_path is None:
            index_path = INDEX_PATH
        if synonyms_path is None:
            synonyms_path = SYNONYMS_PATH

        self.index_path = index_path
        self.tokenizer = ChineseTokenizer(str(synonyms_path) if synonyms_path.exists() else None)
        self.documents = []
        self.bm25_index = {}  # domain → BM25 instance
        self.doc_lookup = {}  # doc_id → full doc
        self._loaded = False
        self._embedder = None     # ChromaDB 语义搜索（懒加载）
        self._hybrid = False

    def enable_hybrid(self) -> "KnowledgeSearchEngine":
        """启用 hybrid BM25+Semantic 搜索（懒加载 Embedder）。"""
        self._hybrid = True
        return self

    def enable_semantic_only(self) -> "KnowledgeSearchEngine":
        """启用纯语义搜索（不调 BM25）。"""
        self._hybrid = "semantic_only"
        return self

    @property
    def embedder(self):
        if self._embedder is None:
            from embedder import Embedder
            self._embedder = Embedder()
        return self._embedder

    def load(self):
        """加载 JSON 索引并构建 BM25"""
        if self._loaded:
            return self

        with open(self.index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)

        self.documents = index_data["documents"]
        for doc in self.documents:
            self.doc_lookup[doc["id"]] = doc

        # 按域分组构建 BM25
        domain_docs = defaultdict(list)
        for doc in self.documents:
            dtype = doc["type"]
            config = DOMAIN_CONFIG.get(dtype, {})
            search_cols = config.get("search_on", ["title", "sections_text"])

            # 构建搜索文本
            search_text_parts = []
            for col in search_cols:
                if col == "sections_text":
                    search_text_parts.append(
                        " ".join(doc.get("sections", {}).values())
                    )
                elif col == "tags_text":
                    search_text_parts.append(" ".join(doc.get("tags", [])))
                else:
                    val = doc.get(col, "")
                    if val:
                        search_text_parts.append(str(val))

            full_text = " ".join(search_text_parts)
            tokens = self.tokenizer.tokenize(full_text)
            domain_docs[dtype].append((doc["id"], tokens))

        for dtype, docs in domain_docs.items():
            bm25 = BM25()
            bm25.fit(docs)
            self.bm25_index[dtype] = bm25

        self._loaded = True
        return self

    # ── MAGMA 意图分类: 查图选路 ──────────────────────
    _INTENT_PATTERNS = {
        "causal": [
            "为什么", "原因", "理由", "动机", "目的", "为啥",
            "因为", "所以", "导致", "引起", "触发", "根源",
        ],
        "temporal": [
            "什么时候", "何时", "先后", "顺序", "流程", "步骤",
            "之前", "之后", "最早", "最近", "历史", "演变",
        ],
        "entity": [
            "谁", "哪个", "哪些", "什么人", "什么文件",
            "在哪里", "文件", "模块", "函数", "类", "路径",
        ],
    }

    _INTENT_DOMAIN_BOOST = {
        "causal": {"decision": 3, "insight": 2, "principle": 1},
        "temporal": {"case": 2, "experiment": 2, "question": 1},
        "entity": {"case": 3, "reference": 2, "experiment": 1},
    }

    def detect_intent(self, query: str) -> str:
        """MAGMA 意图分类: causal | temporal | entity | semantic (默认)。

        用于决定优先查哪个"图"——域路由的偏置。
        """
        query_lower = query.lower()
        scores = {}
        for intent, keywords in self._INTENT_PATTERNS.items():
            scores[intent] = sum(1 for kw in keywords if kw in query_lower)

        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else "semantic"

    def detect_domain(self, query: str) -> str:
        """关键词命中 + MAGMA 意图偏置 → 自动判域，默认返回 'case'"""
        query_lower = query.lower()
        intent = self.detect_intent(query)
        intent_boost = self._INTENT_DOMAIN_BOOST.get(intent, {})

        scores = {}
        for domain, keywords in INTENT_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in query_lower)
            # 前缀匹配: D008, P001 等
            if re.search(rf"\b{domain[:1]}\d{{3}}\b", query_lower):
                score += 5
            # MAGMA 意图偏置: 按查询意图加权
            score += intent_boost.get(domain, 0)
            scores[domain] = score

        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else "case"

    def search(
        self, query: str, domain: str = None, max_results: int = MAX_RESULTS
    ) -> dict:
        """主搜索入口"""
        if not self._loaded:
            self.load()

        if domain is None:
            domain = self.detect_domain(query)

        # 在指定域搜索
        bm25 = self.bm25_index.get(domain)
        if bm25 is None:
            # 回退到全局搜索
            return self._search_all(query, max_results)

        query_tokens = self.tokenizer.tokenize(query)
        if not query_tokens:
            return {"domain": domain, "query": query, "results": [], "count": 0}

        ranked = bm25.score(query_tokens)
        results = []
        for doc_id, score in ranked[:max_results]:
            if score > 0:
                doc = self.doc_lookup.get(doc_id, {})
                results.append(self._format_result(doc, score))

        # ── 语义 / Hybrid 搜索 ──────────────────────
        mode = "bm25"
        if self._hybrid:
            try:
                semantic_results = self.embedder.search(
                    query, n_results=max_results * 2,
                    domain=domain if domain != "all" else None,
                )
                if self._hybrid == "semantic_only":
                    results = semantic_results[:max_results]
                    mode = "semantic"
                else:
                    from hybrid import rrf_fusion
                    results = rrf_fusion(results, semantic_results)[:max_results]
                    mode = "hybrid"
            except Exception:
                # 语义搜索失败 → 静默回退 BM25
                pass

        return {
            "domain": domain,
            "domain_label": DOMAIN_CONFIG.get(domain, {}).get("label", domain),
            "query": query,
            "query_tokens": query_tokens,
            "intent": self.detect_intent(query),
            "count": len(results),
            "results": results,
            "mode": mode,
        }

    def _search_all(self, query: str, max_results: int) -> dict:
        """跨域搜索（回退）"""
        query_tokens = self.tokenizer.tokenize(query)
        all_candidates = []
        for dtype, bm25 in self.bm25_index.items():
            ranked = bm25.score(query_tokens)
            for doc_id, score in ranked[:3]:
                if score > 0:
                    doc = self.doc_lookup.get(doc_id, {})
                    all_candidates.append((score, self._format_result(doc, score)))

        all_candidates.sort(key=lambda x: -x[0])
        results = [r[1] for r in all_candidates[:max_results]]

        return {
            "domain": "all",
            "domain_label": "全局",
            "query": query,
            "query_tokens": query_tokens,
            "count": len(results),
            "results": results,
        }

    def _format_result(self, doc: dict, score: float) -> dict:
        """格式化单条结果"""
        result = {
            "id": doc["id"],
            "type": doc["type"],
            "title": doc["title"],
            "path": doc["path"],
            "score": round(score, 4),
            "positioning": doc.get("positioning", ""),
            "tags": doc.get("tags", []),
            "status": doc.get("status", {}),
            "wikilinks": doc.get("wikilinks", []),
        }

        # 可解释字段
        sections = doc.get("sections", {})
        if sections:
            result["section_count"] = len(sections)

        return result

    def get_neighbors(self, doc_id: str, max_results: int = 5) -> list:
        """获取文档的 wikilink 邻居"""
        doc = self.doc_lookup.get(doc_id)
        if not doc:
            return []

        neighbors = []
        for link_id in doc.get("wikilinks", []):
            num_match = re.search(r"(\d{3})", link_id)
            target = num_match.group(1) if num_match else link_id
            neighbor = self.doc_lookup.get(target)
            if neighbor:
                neighbors.append(
                    {
                        "id": target,
                        "title": neighbor["title"],
                        "type": neighbor["type"],
                    }
                )

            if len(neighbors) >= max_results:
                break

        return neighbors
