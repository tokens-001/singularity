from __future__ import annotations
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChromaDB 语义搜索嵌入器
========================
- 管理 sentence-transformers 模型加载（bge-small-zh-v1.5）
- ChromaDB collection 创建/查询/增删
- 增量索引 manifest（doc_id → SHA256）
- 语义搜索接口
"""

import json
import hashlib
from pathlib import Path
from typing import Optional

from config import CHROMA_DIR, CHROMA_COLLECTION, EMBEDDING_MODEL, MANIFEST_PATH


class Embedder:
    """ChromaDB 嵌入器：模型 + 向量库 + manifest"""

    def __init__(
        self,
        model_name: str = EMBEDDING_MODEL,
        chroma_path: Path = CHROMA_DIR,
        collection_name: str = CHROMA_COLLECTION,
    ):
        self.model_name = model_name
        self.chroma_path = Path(chroma_path)
        self.collection_name = collection_name
        self._model = None
        self._collection = None

    # ── 懒加载 ──────────────────────────────────────────

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return self._model

    @property
    def collection(self):
        if self._collection is None:
            import chromadb
            self.chroma_path.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(self.chroma_path))
            try:
                self._collection = client.get_collection(self.collection_name)
            except Exception:
                self._collection = client.create_collection(
                    name=self.collection_name,
                    metadata={"hnsw:space": "cosine"},
                )
        return self._collection

    # ── 文档嵌入 ────────────────────────────────────────

    @staticmethod
    def _embedding_text(doc: dict) -> str:
        """从文档 dict 拼出用于 embedding 的文本。"""
        parts = [
            doc.get("title", ""),
            doc.get("positioning", ""),
            doc.get("sections_text", ""),
            doc.get("tags_text", ""),
        ]
        return " ".join(p for p in parts if p)

    @staticmethod
    def _text_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def embed_documents(self, documents: list[dict]) -> int:
        """批量嵌入文档（增量：只嵌入 manifest 中不匹配的）。
        返回本次嵌入数。
        """
        manifest = load_manifest()
        to_embed_ids = []
        to_embed_texts = []
        to_embed_metadatas = []
        current_ids = set()

        for doc in documents:
            doc_id = doc["id"]
            current_ids.add(doc_id)
            text = self._embedding_text(doc)
            new_hash = self._text_hash(text)

            if doc_id in manifest and manifest[doc_id] == new_hash:
                continue  # 未变，跳过

            to_embed_ids.append(doc_id)
            to_embed_texts.append(text)
            to_embed_metadatas.append({
                "type": doc.get("type", ""),
                "title": doc.get("title", ""),
                "path": doc.get("path", ""),
                "positioning": doc.get("positioning", ""),
                "tags": ", ".join(doc.get("tags", [])),
                "status": str(doc.get("status", "")),
            })
            manifest[doc_id] = new_hash

        # 删掉 manifest 中有但 corpus 中已删的文档
        removed = [did for did in manifest if did not in current_ids]
        for did in removed:
            self.delete_document(did)
        for did in removed:
            del manifest[did]

        if to_embed_ids:
            vectors = self.model.encode(
                to_embed_texts,
                batch_size=32,
                show_progress_bar=False,
            ).tolist()
            self.collection.upsert(
                ids=to_embed_ids,
                embeddings=vectors,
                metadatas=to_embed_metadatas,
            )

        save_manifest(manifest)
        return len(to_embed_ids)

    # ── 语义搜索 ────────────────────────────────────────

    def search(
        self, query: str, n_results: int = 10, domain: Optional[str] = None
    ) -> list[dict]:
        """语义搜索，返回标准化结果列表。"""
        vec = self.model.encode([query], show_progress_bar=False).tolist()
        where = {"type": domain} if domain else None
        results = self.collection.query(
            query_embeddings=vec,
            n_results=min(n_results, self.count()),
            where=where,
        )

        docs = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                distance = results["distances"][0][i] if results["distances"] else 0
                docs.append({
                    "id": doc_id,
                    "type": meta.get("type", ""),
                    "title": meta.get("title", ""),
                    "path": meta.get("path", ""),
                    "score": round(1.0 - distance, 4),  # cosine distance → similarity
                    "positioning": meta.get("positioning", ""),
                    "tags": meta.get("tags", "").split(", ") if meta.get("tags") else [],
                    "status": meta.get("status", ""),
                    "source": "semantic",
                })
        return docs

    # ── 管理 ────────────────────────────────────────────

    def delete_document(self, doc_id: str) -> None:
        try:
            self.collection.delete(ids=[doc_id])
        except Exception:
            pass

    def count(self) -> int:
        return self.collection.count()


# ── Manifest 管理 ──────────────────────────────────────

def load_manifest() -> dict[str, str]:
    """加载 {doc_id: sha256} manifest。"""
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def save_manifest(manifest: dict[str, str]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
