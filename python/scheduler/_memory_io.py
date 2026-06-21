"""_memory_io.py — Memory I/O primitives shared across memory sub-modules."""
from __future__ import annotations
import json
import os
from pathlib import Path

from . import config as sched_config

_MEMORY_DIR = sched_config.QIDIAN_DIR / "memory"
_EVENTS_PATH = _MEMORY_DIR / "events.json"
_EDGES_PATH = _MEMORY_DIR / "edges.json"
_ENTITY_IDX_PATH = _MEMORY_DIR / "entity_index.json"


def ensure_dir() -> None:
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict | list:
    if not path.exists():
        return {} if ".json" in str(path) else []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {} if ".json" in str(path) else []


def write_json(path: Path, data: dict | list) -> None:
    ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))
