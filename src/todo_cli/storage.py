"""
Persistent storage layer with atomic writes and crash recovery.

Strategy:
  - write-to-temp-then-rename for atomic file updates
  - backup file maintained for recovery on corruption
  - JSONDecodeError on load triggers automatic recovery
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Storage:
    """JSON file storage with atomic writes and automatic recovery."""

    def __init__(self, filepath: str):
        self._filepath = Path(filepath)
        self._backup_path = self._filepath.with_suffix(".json.bak")
        self._dir = self._filepath.parent
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def filepath(self) -> str:
        return str(self._filepath)

    def load(self) -> list[dict[str, Any]]:
        """Load data from JSON file.

        Recovery order:
          1. Try primary file
          2. Try backup file if primary is corrupted
          3. Return empty list if both fail
        """
        # Try primary
        data = self._read_json(self._filepath)
        if data is not None:
            return data

        # Primary corrupted — log and try backup
        logger.error("Primary file corrupted: %s, attempting backup recovery", self._filepath)
        data = self._read_json(self._backup_path)
        if data is not None:
            logger.info("Recovered from backup: %s", self._backup_path)
            # Restore primary from backup
            self.save(data)
            return data

        # Both failed — initialize empty
        logger.error("Both primary and backup files unreadable, initializing empty data")
        return []

    def save(self, data: list[dict[str, Any]]) -> None:
        """Atomically save data to JSON file.

        Uses write-to-temp-then-rename strategy:
          1. Write to a temp file in the same directory
          2. fsync to ensure data is on disk
          3. Rename temp → backup (atomic on same filesystem)
          4. Rename temp → primary (atomic on same filesystem)
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._dir),
            prefix=".todo_tmp_",
            suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())

            tmp_path_obj = Path(tmp_path)

            # Write backup first (atomic rename)
            if self._backup_path.exists():
                self._backup_path.unlink()
            os.replace(str(tmp_path_obj), str(self._backup_path))

            # Now write to primary via another temp file
            fd2, tmp_path2 = tempfile.mkstemp(
                dir=str(self._dir),
                prefix=".todo_tmp_",
                suffix=".json"
            )
            with os.fdopen(fd2, "w", encoding="utf-8") as f2:
                json.dump(data, f2, ensure_ascii=False, indent=2)
                f2.flush()
                os.fsync(f2.fileno())

            tmp_path2_obj = Path(tmp_path2)
            if self._filepath.exists():
                self._filepath.unlink()
            os.replace(str(tmp_path2_obj), str(self._filepath))

        except Exception:
            # Clean up temp file on failure
            for p in [tmp_path]:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def _read_json(self, path: Path) -> list[dict[str, Any]] | None:
        """Read and parse a JSON file. Returns None on any failure."""
        try:
            if not path.exists():
                return None
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return None
                data = json.loads(content)
                if not isinstance(data, list):
                    logger.error("Expected list in %s, got %s", path, type(data).__name__)
                    return None
                return data
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.error("Failed to read %s: %s", path, e)
            return None
