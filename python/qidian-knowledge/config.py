#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置中心
=========
语料根路径、BM25 参数、域→目录映射
"""

from pathlib import Path

# 项目根
PROJECT_ROOT = Path(__file__).parent.parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
RESEARCH_DIR = PROJECT_ROOT / "research"

# 索引
DATA_DIR = Path(__file__).parent / "data"
INDEX_DIR = DATA_DIR / "index"
CORPUS_PATH = INDEX_DIR / "corpus.json"
SYNONYMS_PATH = DATA_DIR / "synonyms.csv"
GOLDEN_SET_PATH = DATA_DIR / "golden_set.jsonl"

# 搜索引擎
BM25_K1 = 1.5
BM25_B = 1.5  # 比默认 0.75 高，中文长文需要更宽容
MAX_SEARCH_RESULTS = 5

# ChromaDB 语义搜索
CHROMA_DIR = DATA_DIR / "chroma"
CHROMA_COLLECTION = "qidian_knowledge"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
MANIFEST_PATH = CHROMA_DIR / ".manifest.json"

# 校验器
HARD_CONFLICT_PENALTY = 30
SOFT_WARNING_PENALTY = 5
VALIDATION_THRESHOLD_PASS = 80
VALIDATION_THRESHOLD_WARN = 50
