"""持久化层：负责本地 JSON 数据的读写与备份恢复。"""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class StorageError(Exception):
    pass


def _atomic_write_json(path: Path, data: Any) -> None:
    """原子写入：先写入临时文件，再 rename 替换目标文件。"""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


def _backup_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".bak")


class Storage:
    """Storage 接口契约：
    - load() -> list[dict]
    - save(list[dict]) -> None
    """

    def __init__(self, data_path: str | Path) -> None:
        self.data_path = Path(data_path)
        self.backup_path = _backup_path(self.data_path)

    def load(self) -> list[dict[str, Any]]:
        """从主文件加载；若 JSON 损坏，尝试从备份恢复；否则返回空列表。"""
        if self.data_path.exists():
            try:
                with self.data_path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                    if isinstance(raw, list):
                        return raw
            except json.JSONDecodeError as exc:
                logger.error("JSON decode error in %s: %s", self.data_path, exc)
                if self.backup_path.exists():
                    try:
                        with self.backup_path.open("r", encoding="utf-8") as f:
                            raw = json.load(f)
                            if isinstance(raw, list):
                                logger.info("Recovered from backup %s", self.backup_path)
                                return raw
                    except Exception as bexc:
                        logger.error("Backup also corrupted: %s", bexc)
            except Exception as exc:
                logger.error("Failed to load %s: %s", self.data_path, exc)
        return []

    def save(self, data: list[dict[str, Any]]) -> None:
        """原子写入并更新备份。"""
        try:
            _atomic_write_json(self.data_path, data)
            if self.data_path.exists():
                shutil.copy2(self.data_path, self.backup_path)
        except Exception as exc:
            raise StorageError(f"Failed to save data: {exc}") from exc
