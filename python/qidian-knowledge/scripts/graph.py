#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识图谱
=========
wikilink 图 → 邻居扩散 + 路径查找 + Hub 节点识别
依赖 corpus.json 索引中已有的 graph 数据
"""

import json
from pathlib import Path
from collections import defaultdict, deque

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "data"
INDEX_PATH = DATA_DIR / "index" / "corpus.json"


class KnowledgeGraph:
    """基于 wikilink 的知识图谱"""

    def __init__(self, index_path: Path = None):
        if index_path is None:
            index_path = INDEX_PATH

        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)

        graph_data = index_data.get("graph", {})
        self.out_edges = defaultdict(set)  # A → {B, C}
        self.in_edges = defaultdict(set)  # A ← {X, Y}
        self.documents = {d["id"]: d for d in index_data["documents"]}

        for src, targets in graph_data.get("out_edges", {}).items():
            for tgt in targets:
                self.out_edges[src].add(tgt)
                self.in_edges[tgt].add(src)

    def get_neighbors(self, doc_id: str, depth: int = 1, hub_damping: bool = True) -> dict:
        """获取文档的邻居（出链 + 入链），可指定深度。

        hub_damping=True 时对高入度节点（≥5入链）做衰减——hub 节点
        几乎什么都连，扩散时稀释精度。衰减后 hub 仍可被遍历但不
        进入邻居列表。
        """
        # 预计算 hub 节点集
        hub_nodes = set()
        if hub_damping:
            for nid, in_links in self.in_edges.items():
                if len(in_links) >= 5:
                    hub_nodes.add(nid)

        visited = {doc_id}
        current = {doc_id}
        all_neighbors = defaultdict(set)

        for d in range(depth):
            next_level = set()
            for node in current:
                # 出链
                for tgt in self.out_edges.get(node, set()):
                    if tgt not in visited:
                        visited.add(tgt)
                        next_level.add(tgt)
                        if tgt not in hub_nodes:
                            all_neighbors["out"].add(tgt)
                # 入链
                for src in self.in_edges.get(node, set()):
                    if src not in visited:
                        visited.add(src)
                        next_level.add(src)
                        if src not in hub_nodes:
                            all_neighbors["in"].add(src)
            current = next_level

        result = {"doc_id": doc_id, "depth": depth}
        doc = self.documents.get(doc_id, {})
        result["title"] = doc.get("title", "")
        result["type"] = doc.get("type", "")

        for direction in ["out", "in"]:
            neighbor_ids = all_neighbors.get(direction, set())
            neighbors = []
            for nid in sorted(neighbor_ids):
                ndoc = self.documents.get(nid, {})
                neighbors.append(
                    {
                        "id": nid,
                        "title": ndoc.get("title", ""),
                        "type": ndoc.get("type", ""),
                    }
                )
            result[f"{direction}_links"] = neighbors
            result[f"{direction}_count"] = len(neighbors)

        # 标注被衰减的 hub 节点
        if hub_damping:
            damped = [n for n in visited if n in hub_nodes and n != doc_id]
            if damped:
                result["hub_damped"] = damped

        return result

    def find_path(self, from_id: str, to_id: str, max_depth: int = 5) -> list:
        """BFS 找最短路径"""
        if from_id == to_id:
            return [from_id]
        if from_id not in self.documents or to_id not in self.documents:
            return []

        queue = deque([(from_id, [from_id])])
        visited = {from_id}

        while queue:
            current, path = queue.popleft()
            if len(path) > max_depth:
                continue

            neighbors = (
                self.out_edges.get(current, set()) | self.in_edges.get(current, set())
            )
            for neighbor in neighbors:
                if neighbor == to_id:
                    return path + [neighbor]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

        return []

    def get_hubs(self, min_degree: int = 3) -> list:
        """获取 Hub 节点（被引用次数 ≥ min_degree）"""
        hubs = []
        for node_id, in_links in self.in_edges.items():
            out_links = self.out_edges.get(node_id, set())
            degree = len(in_links) + len(out_links)
            if len(in_links) >= min_degree:
                doc = self.documents.get(node_id, {})
                hubs.append(
                    {
                        "id": node_id,
                        "title": doc.get("title", ""),
                        "type": doc.get("type", ""),
                        "in_degree": len(in_links),
                        "out_degree": len(out_links),
                        "total_degree": degree,
                    }
                )
        return sorted(hubs, key=lambda h: -h["in_degree"])

    def get_connected_component(self, doc_id: str) -> dict:
        """获取文档所在的连通分量"""
        if doc_id not in self.documents:
            return {"doc_id": doc_id, "nodes": [], "edges": [], "size": 0}

        visited = set()
        queue = deque([doc_id])
        visited.add(doc_id)

        while queue:
            current = queue.popleft()
            neighbors = (
                self.out_edges.get(current, set()) | self.in_edges.get(current, set())
            )
            for n in neighbors:
                if n not in visited:
                    visited.add(n)
                    queue.append(n)

        nodes = []
        for nid in sorted(visited):
            doc = self.documents.get(nid, {})
            nodes.append(
                {"id": nid, "title": doc.get("title", ""), "type": doc.get("type", "")}
            )

        edges = []
        for src in visited:
            for tgt in self.out_edges.get(src, set()):
                if tgt in visited:
                    edges.append([src, tgt])

        return {
            "doc_id": doc_id,
            "size": len(nodes),
            "nodes": nodes,
            "edges": edges,
        }
