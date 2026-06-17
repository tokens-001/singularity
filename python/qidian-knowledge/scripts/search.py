#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识搜索 CLI
=============
用法:
  python scripts/search.py "<query>"                      # 自动判域
  python scripts/search.py "<query>" --domain principle   # 指定域
  python scripts/search.py "<query>" --neighbors          # 含邻居

域: principle / decision / insight / case / question / experiment / all
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))  # config.py / embedder 需要

from core import KnowledgeSearchEngine, DOMAIN_CONFIG

AVAILABLE_DOMAINS = list(DOMAIN_CONFIG.keys()) + ["all"]


def format_result(search_result: dict, show_neighbors: bool = False, engine=None):
    """格式化输出搜索结果（token 优化）"""
    if not search_result.get("results"):
        return f"  (无匹配)  域: {search_result.get('domain_label', '')}"

    lines = []
    lines.append(
        f"## 搜索: {search_result['query']}"
    )
    lines.append(
        f"**域:** {search_result.get('domain_label', 'all')} "
        f"| **命中:** {search_result['count']} 条"
    )
    lines.append("")

    for i, r in enumerate(search_result["results"], 1):
        score = r.get("score", 0)
        status_val = r.get("status", {}).get("value", "")
        lines.append(f"### {i}. {r['title']}")
        lines.append(f"- **类型:** {r['type']} | **ID:** {r['id']} | **分数:** {score}")
        if r.get("positioning"):
            pos = r["positioning"]
            if len(pos) > 200:
                pos = pos[:200] + "..."
            lines.append(f"- **定位:** {pos}")
        if r.get("tags"):
            lines.append(f"- **标签:** {', '.join(r['tags'])}")
        if status_val:
            lines.append(f"- **状态:** {status_val}")
        lines.append(f"- **路径:** `{r['path']}`")

        # 邻居扩散
        if show_neighbors and engine and r.get("wikilinks"):
            neighbors = engine.get_neighbors(r["id"])
            if neighbors:
                n_ids = [f"{n['id']}({n['type']})" for n in neighbors[:5]]
                lines.append(f"- **关联:** {', '.join(n_ids)}")

        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="奇点知识搜索")
    parser.add_argument("query", help="搜索查询")
    parser.add_argument(
        "--domain",
        "-d",
        choices=AVAILABLE_DOMAINS,
        default=None,
        help="搜索域 (默认自动判域)",
    )
    parser.add_argument(
        "--max-results",
        "-n",
        type=int,
        default=5,
        help="最大结果数 (默认 5)",
    )
    parser.add_argument(
        "--neighbors",
        action="store_true",
        help="显示 wikilink 邻居",
    )
    parser.add_argument(
        "--hybrid",
        action="store_true",
        help="BM25 + 语义 RRF 融合搜索",
    )
    parser.add_argument(
        "--semantic",
        action="store_true",
        help="纯语义搜索（不调 BM25）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON",
    )
    args = parser.parse_args()

    engine = KnowledgeSearchEngine()
    engine.load()

    if args.semantic:
        engine.enable_semantic_only()
    elif args.hybrid:
        engine.enable_hybrid()

    result = engine.search(args.query, domain=args.domain, max_results=args.max_results)

    if args.json:
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_result(result, show_neighbors=args.neighbors, engine=engine))
