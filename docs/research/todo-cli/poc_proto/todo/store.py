"""PoC 原型：JSON 文件读写（仅用于技术预研验证，不是交付物）。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(os.path.expanduser("~")) / ".todo.json"


def _path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path) if path is not None else DEFAULT_PATH


def _quarantine(p: Path) -> None:
    """损坏文件改名备份，保证用户数据不静默丢失。"""
    try:
        shutil.move(str(p), f"{p}.corrupt.{int(time.time())}")
    except OSError:
        pass


def _empty() -> dict[str, Any]:
    return {"version": 1, "next_id": 1, "items": []}


def _read(p: Path) -> dict[str, Any]:
    if not p.exists():
        return _empty()
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        _quarantine(p)
        return _empty()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _quarantine(p)
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        if isinstance(data, list):  # 容忍裸数组格式
            items = [i for i in data if isinstance(i, dict)]
            return {"version": 1, "next_id": _next(items), "items": items}
        _quarantine(p)
        return _empty()
    items = [i for i in data["items"] if isinstance(i, dict) and "id" in i]
    return {"version": 1, "next_id": int(data.get("next_id") or _next(items)), "items": items}


def _next(items: list[dict[str, Any]]) -> int:
    return max((int(i["id"]) for i in items if isinstance(i.get("id"), int)), default=0) + 1


def _write(p: Path, data: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".todo.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _find(data: dict[str, Any], item_id: int) -> dict[str, Any] | None:
    return next((i for i in data["items"] if i.get("id") == item_id), None)


def add(text: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("task text must not be empty")
    p = _path(path)
    data = _read(p)
    item = {"id": data["next_id"], "text": text, "done": False, "created": time.time()}
    data["items"].append(item)
    data["next_id"] = item["id"] + 1
    _write(p, data)
    return item


def list(path=None, include_done: bool = True) -> list[dict[str, Any]]:  # noqa: A001
    data = _read(_path(path))
    items = data["items"]
    return items if include_done else [i for i in items if not i.get("done")]


def done(item_id: int, path=None) -> dict[str, Any] | None:
    p = _path(path)
    data = _read(p)
    item = _find(data, int(item_id))
    if item is None:
        return None
    item["done"] = True
    _write(p, data)
    return item


def remove(item_id: int, path=None) -> bool:
    p = _path(path)
    data = _read(p)
    before = len(data["items"])
    data["items"] = [i for i in data["items"] if i.get("id") != int(item_id)]
    if len(data["items"]) == before:
        return False
    _write(p, data)
    return True
