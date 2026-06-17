#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 0 · 摄取骨架
===============
把 knowledge/ 和 research/ 两套 Markdown 语料统一解析成规范化文档列表。
输出：id / type / tags / sections / wikilinks / status / metadata 等字段。
"""

import json
import re
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# ============ CONFIGURATION ============
PROJECT_ROOT = Path(__file__).parent.parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
RESEARCH_DIR = PROJECT_ROOT / "research"
DATA_DIR = Path(__file__).parent / "data"

# 文档类型映射
RESEARCH_TYPE_MAP = {
    "decisions": "decision",
    "insights": "insight",
    "questions": "question",
    "experiments": "experiment",
    "references": "reference",
}

# 前缀与类型映射（knowledge/ 目录下的编号前缀）
PREFIX_TYPE_MAP = {
    "P": "principle",
    "D": "decision",
    "I": "insight",
    "Q": "question",
    "E": "experiment",
}


def parse_frontmatter(text: str) -> tuple:
    """解析 YAML 风格 frontmatter（--- ... ---）+ 返回 (metadata, body)"""
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    fm_text = parts[1].strip()
    body = parts[2].strip()

    metadata = {}
    # tags: [tag1, tag2]
    tags_match = re.search(r"tags:\s*\[([^\]]+)\]", fm_text)
    if tags_match:
        metadata["tags"] = [t.strip() for t in tags_match.group(1).split(",")]

    # 其他 frontmatter 字段
    for key in ["status", "type", "priority"]:
        match = re.search(rf"{key}:\s*(.+)", fm_text)
        if match:
            metadata[key] = match.group(1).strip()

    return metadata, body


def extract_id(body: str, filename: str) -> str:
    """从 body 中提取编号，或从文件名推断"""
    id_match = re.search(r"编号[：:]\s*(\S+)", body)
    if id_match:
        return id_match.group(1).strip()

    # 从文件名提取：P001_xxx → P001, D008_xxx → D008, 001-xxx → 001
    stem = Path(filename).stem
    prefix_match = re.match(r"([A-Z])(\d{3}[a-z]?)", stem)
    if prefix_match:
        return prefix_match.group(0)

    num_match = re.match(r"(\d{3})", stem)
    if num_match:
        return num_match.group(0)

    return stem


def extract_archived_date(body: str) -> str:
    """提取归档日期"""
    match = re.search(r"归档日期[：:]\s*(.+)$", body, re.MULTILINE)
    return match.group(1).strip() if match else ""


def extract_positioning(body: str) -> str:
    """提取定位描述"""
    match = re.search(r"定位[：:]\s*(.+)$", body, re.MULTILINE)
    return match.group(1).strip() if match else ""


def extract_wikilinks(body: str) -> list:
    """提取所有 [[wikilink]]"""
    return re.findall(r"\[\[([^\]]+)\]\]", body)


def extract_sections(body: str) -> dict:
    """把 Markdown 按 ## 标题切块"""
    sections = {}
    current_h2 = None
    current_content = []

    for line in body.split("\n"):
        h2_match = re.match(r"^##\s+(.+)$", line)
        if h2_match:
            if current_h2:
                sections[current_h2] = "\n".join(current_content).strip()
            current_h2 = h2_match.group(1).strip()
            current_content = []
        else:
            current_content.append(line)

    if current_h2:
        sections[current_h2] = "\n".join(current_content).strip()

    return sections


def extract_status(body: str) -> dict:
    """提取状态相关信息"""
    status = {"value": "unknown", "has_user_validation": False, "has_counterexample": False}

    # 状态行：状态：自测通过（0真实用户）
    status_match = re.search(r"状态[：:]\s*(.+)$", body, re.MULTILINE)
    if status_match:
        status["value"] = status_match.group(1).strip()

    # 是否有真实用户验证
    if "真实用户" in body or "0真实用户" in body:
        status["has_user_validation"] = "0真实用户" not in body
    if "已验证" in body and "自测" not in body:
        status["has_user_validation"] = True

    # 是否有反例段
    if "反例" in body or "不适" in body:
        status["has_counterexample"] = True

    return status


def extract_title(body: str, filename: str) -> str:
    """提取文档标题"""
    h1_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if h1_match:
        return h1_match.group(1).strip()

    # fallback: 文件头第一行去掉 # 前缀
    first_line = body.split("\n")[0]
    return first_line.lstrip("# ").strip() or Path(filename).stem


def determine_type(filepath: Path, doc_id: str, body: str) -> str:
    """确定文档类型"""
    rel_path = str(filepath)

    # research/ 按子目录定类型
    if "research/decisions/" in rel_path:
        return "decision"
    if "research/insights/" in rel_path:
        # P 前缀的文件在 research/insights/ 下但属于 principle
        if doc_id.startswith("P") and doc_id[1:].isdigit():
            return "principle"
        return "insight"
    if "research/questions/" in rel_path:
        return "question"
    if "research/experiments/" in rel_path:
        return "experiment"
    if "research/references/" in rel_path:
        return "reference"

    # knowledge/ 按编号前缀
    if re.match(r"^[A-Z]", doc_id):
        prefix = doc_id[0]
        return PREFIX_TYPE_MAP.get(prefix, "case")

    return "case"


def ingest_file(filepath: Path) -> dict:
    """摄取单个 Markdown 文件"""
    rel_path = str(filepath.relative_to(PROJECT_ROOT))

    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()

    fm_meta, body = parse_frontmatter(text)
    doc_id = extract_id(body, filepath.name)
    doc_type = determine_type(filepath, doc_id, body)
    wikilinks = extract_wikilinks(body)

    doc = {
        "id": doc_id,
        "path": rel_path,
        "filename": filepath.name,
        "type": doc_type,
        "title": extract_title(body, filepath.name),
        "tags": fm_meta.get("tags", []),
        "positioning": extract_positioning(body),
        "archived_date": extract_archived_date(body),
        "status": extract_status(body),
        "wikilinks": wikilinks,
        "sections": extract_sections(body),
        "char_count": len(body),
        "line_count": body.count("\n") + 1,
    }

    return doc


def build_graph(documents: list) -> dict:
    """从文档的 wikilinks 构建出入链图"""
    in_edges = defaultdict(set)
    out_edges = {}

    for doc in documents:
        doc_id = doc["id"]
        # 规范化文件名作为链接目标
        targets = set()
        for link in doc["wikilinks"]:
            # [[007-真实暴露与分级筛选]] → 007
            num_match = re.match(r"(\d{3})", link)
            if num_match:
                targets.add(num_match.group(1))
            else:
                # [[P001_验证层优先]] → P001
                prefix_match = re.match(r"([A-Z]\d{3})", link)
                if prefix_match:
                    targets.add(prefix_match.group(1))
                else:
                    targets.add(link)

        out_edges[doc_id] = targets
        for target in targets:
            in_edges[target].add(doc_id)

    return {
        "out_edges": {k: list(v) for k, v in out_edges.items()},
        "in_edges": {k: list(v) for k, v in in_edges.items()},
        "hub_nodes": [(k, len(v)) for k, v in in_edges.items() if len(v) >= 3],
    }


def ingest_all() -> dict:
    """摄取全部语料，返回索引数据"""
    documents = []

    # knowledge/ 目录
    if KNOWLEDGE_DIR.exists():
        for md_file in sorted(KNOWLEDGE_DIR.glob("*.md")):
            if md_file.name in ("体系边疆.md", "knowledge_map.md"):
                continue  # 跳过索引文件
            try:
                doc = ingest_file(md_file)
                documents.append(doc)
            except Exception as e:
                print(f"  ⚠ 跳过 {md_file.name}: {e}")

    # research/ 子目录
    if RESEARCH_DIR.exists():
        for subdir in sorted(RESEARCH_DIR.iterdir()):
            if not subdir.is_dir():
                continue
            for md_file in sorted(subdir.glob("*.md")):
                try:
                    doc = ingest_file(md_file)
                    documents.append(doc)
                except Exception as e:
                    print(f"  ⚠ 跳过 {md_file.name}: {e}")

    # 构建图谱
    graph = build_graph(documents)

    # 统计
    type_counts = defaultdict(int)
    for doc in documents:
        type_counts[doc["type"]] += 1

    return {
        "documents": documents,
        "graph": graph,
        "stats": {
            "total_docs": len(documents),
            "by_type": dict(type_counts),
            "total_wikilinks": sum(len(d["wikilinks"]) for d in documents),
            "hub_nodes": graph["hub_nodes"],
            "ingested_at": datetime.now().isoformat(),
        },
    }


def save_index(index_data: dict, output_path: Path = None):
    """保存索引到 data/index/"""
    if output_path is None:
        output_path = DATA_DIR / "index" / "corpus.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)

    return output_path


def print_stats(index_data: dict):
    """打印索引统计"""
    stats = index_data["stats"]
    print(f"\n{'='*50}")
    print(f"  语料摄取完成")
    print(f"{'='*50}")
    print(f"  总文档数:  {stats['total_docs']}")
    print(f"  wikilinks: {stats['total_wikilinks']}")
    print(f"{'='*50}")
    print(f"  按类型:")
    for dtype, count in sorted(stats["by_type"].items(), key=lambda x: -x[1]):
        print(f"    {dtype:15s} {count:3d} 篇")
    print(f"{'='*50}")
    print(f"  Hub 节点 (被引用 ≥3 次):")
    for node_id, count in stats["hub_nodes"]:
        print(f"    {node_id} → {count} 条入链")
    print(f"{'='*50}\n")


def sync_chroma(index_data: dict) -> int:
    """同步 ChromaDB 向量索引（增量：只嵌入新增/修改的文档）。
    返回本次嵌入文档数。失败打印警告，不影响主流程。
    """
    try:
        from scripts.embedder import Embedder
        embedder = Embedder()
        count = embedder.embed_documents(index_data["documents"])
        if count > 0:
            print(f"  ChromaDB: {count} 篇嵌入/更新 ({embedder.count()} 篇总计)")
        elif count == 0:
            print(f"  ChromaDB: 无变更 ({embedder.count()} 篇总计)")
        return count
    except ImportError:
        print("  ChromaDB: 跳过 (sentence-transformers / chromadb 未安装)")
        return -1
    except Exception as e:
        print(f"  ChromaDB: 同步失败 ({e})")
        return -1


if __name__ == "__main__":
    import sys

    no_chroma = "--no-chroma" in sys.argv

    print("Phase 0 · 摄取骨架")
    print(f"  知识目录: {KNOWLEDGE_DIR}")
    print(f"  研究目录: {RESEARCH_DIR}")
    print()

    index_data = ingest_all()
    output = save_index(index_data)
    print_stats(index_data)
    print(f"  索引文件: {output}")

    if not no_chroma:
        sync_chroma(index_data)
