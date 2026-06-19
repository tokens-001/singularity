"""内部模块 — 简单 TTL 内存缓存。

减少文件 IO: /api/tasks 被前端每秒轮询，每次扫描全量任务文件。
缓存命中直接返回，TTL 内不读磁盘。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional


class TTLStore:
    """TTL 内存缓存。写穿透(写时同步更新缓存)。"""

    def __init__(self, ttl_seconds: float = 2.0):
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> Optional[dict]:
        entry = self._data.get(key)
        if entry and time.time() - entry[0] < self._ttl:
            return entry[1]
        if entry:
            del self._data[key]
        return None

    def set(self, key: str, data: dict) -> None:
        self._data[key] = (time.time(), data)

    def invalidate(self, key: str = "") -> None:
        if key:
            self._data.pop(key, None)
        else:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


# 全局缓存实例
task_cache = TTLStore(ttl_seconds=2.0)
project_cache = TTLStore(ttl_seconds=5.0)
