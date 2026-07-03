"""Storage layer - JSON persistence with atomic writes and crash recovery."""
import json
import logging
import os
from pathlib import Path
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


class Storage:
    """Atomic writes + crash recovery via .bak backup."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.backup_path = str(Path(filepath).with_suffix(".json.bak"))

    def load(self) -> List[Dict[str, Any]]:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                content = f.read()
            if not content.strip():
                raise json.JSONDecodeError("empty file", "", 0)
            data = json.loads(content)
            if not isinstance(data, list):
                raise ValueError("Data must be a list")
            return data
        except (json.JSONDecodeError, ValueError, FileNotFoundError):
            logger.error("Corrupted data file detected; attempting recovery")
            return self._recover()

    def save(self, data: List[Dict[str, Any]]) -> None:
        tmp_path = self.filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(self.filepath):
            os.replace(self.filepath, self.backup_path)
        os.replace(tmp_path, self.filepath)
        # 首次写入时也创建备份
        if not os.path.exists(self.backup_path):
            import shutil
            shutil.copy2(self.filepath, self.backup_path)

    def _recover(self) -> List[Dict[str, Any]]:
        try:
            with open(self.backup_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                logger.info("Recovered %d items from backup", len(data))
                # Restore primary
                self.save(data)
                return data
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass
        return []
