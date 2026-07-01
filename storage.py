"""Storage layer - handles JSON persistence with atomic writes and crash recovery."""
import json
import logging
import os
import tempfile
from typing import List, Dict, Any

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class Storage:
    """Manages persistent storage with atomic writes and automatic recovery."""
    
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.backup_path = filepath + ".backup"
        self.temp_path = filepath + ".tmp"
    
    def load(self) -> List[Dict[str, Any]]:
        """Load data from file with automatic recovery on corruption."""
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if not isinstance(data, list):
                    raise ValueError("Data must be a list")
                return data
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Corrupted data file detected: {e}")
            return self._recover_from_backup()
        except FileNotFoundError:
            logger.info("Data file not found, initializing empty storage")
            return []
    
    def save(self, data: List[Dict[str, Any]]) -> None:
        """Save data using atomic write-to-temp-then-rename strategy."""
        # Write to temporary file first
        with open(self.temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        
        # Create backup of existing file
        if os.path.exists(self.filepath):
            try:
                os.replace(self.filepath, self.backup_path)
            except OSError as e:
                logger.warning(f"Failed to create backup: {e}")
        
        # Atomic rename
        os.replace(self.temp_path, self.filepath)
        logger.info(f"Successfully saved {len(data)} items")
    
    def _recover_from_backup(self) -> List[Dict[str, Any]]:
        """Attempt to recover data from backup file."""
        try:
            with open(self.backup_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    logger.info(f"Recovered {len(data)} items from backup")
                    return data
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Backup recovery failed: {e}")
        
        logger.info("Initializing empty storage")
        return []
