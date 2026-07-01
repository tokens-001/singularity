"""Tests for JsonStorage: atomic writes and backup recovery."""

import json
import os

import pytest

from todo.models import Todo
from todo.storage import JsonStorage


class TestJsonStorageLoad:
    """Test storage load behavior."""

    def test_load_empty_when_no_file(self, storage):
        todos = storage.load()
        assert todos == []

    def test_load_saved_todos(self, storage):
        todos = [
            Todo(id="1", title="First"),
            Todo(id="2", title="Second"),
        ]
        storage.save(todos)
        
        loaded = storage.load()
        assert len(loaded) == 2
        assert loaded[0].title == "First"
        assert loaded[1].title == "Second"

    def test_load_returns_empty_on_corrupted_main_with_no_backup(self, storage):
        """When main file is corrupted and no backup exists, return empty list."""
        storage.data_file.write_text("NOT VALID JSON{{{")
        
        loaded = storage.load()
        assert loaded == []

    def test_load_recovers_from_backup(self, storage):
        """When main file is corrupted, recover from backup."""
        # First save creates a valid state + backup
        todos = [Todo(id="1", title="Backup task")]
        storage.save(todos)
        
        # Corrupt main file
        storage.data_file.write_text("CORRUPTED{{{")
        
        # Load should recover from backup
        loaded = storage.load()
        assert len(loaded) == 1
        assert loaded[0].title == "Backup task"
        
        # Main file should be restored
        restored = json.loads(storage.data_file.read_text())
        assert len(restored) == 1

    def test_load_returns_empty_when_both_files_corrupted(self, storage):
        """When both main and backup are corrupted, return empty list."""
        storage.data_file.write_text("BAD JSON 1")
        storage.backup_file.write_text("BAD JSON 2")
        
        loaded = storage.load()
        assert loaded == []


class TestJsonStorageSave:
    """Test storage save behavior (atomic writes)."""

    def test_save_creates_file(self, storage):
        storage.save([])
        assert storage.data_file.exists()

    def test_save_creates_backup_on_second_write(self, storage):
        storage.save([Todo(id="1", title="First")])
        assert not storage.backup_file.exists()
        
        storage.save([Todo(id="1", title="First"), Todo(id="2", title="Second")])
        assert storage.backup_file.exists()

    def test_save_is_valid_json(self, storage):
        todos = [Todo(id="1", title="Test")]
        storage.save(todos)
        
        data = json.loads(storage.data_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["title"] == "Test"

    def test_save_validates_todos(self, storage):
        """Save should validate all todos before writing."""
        bad_todo = Todo(id="1", title="")  # Empty title
        
        with pytest.raises(Exception):  # ValidationError
            storage.save([bad_todo])

    def test_no_temp_files_left_after_save(self, storage):
        """Atomic write should not leave temp files behind."""
        storage.save([Todo(id="1", title="Test")])
        
        # Check no .tmp_ files in directory
        temp_files = list(storage.data_file.parent.glob(".tmp_*"))
        assert len(temp_files) == 0

    def test_save_overwrites_previous_data(self, storage):
        storage.save([Todo(id="1", title="Old")])
        storage.save([Todo(id="2", title="New")])
        
        loaded = storage.load()
        assert len(loaded) == 1
        assert loaded[0].title == "New"


class TestAtomicWrite:
    """Test the atomic write mechanism."""

    def test_atomic_write_creates_file(self, tmp_path):
        storage = JsonStorage(tmp_path / "test.json")
        storage._atomic_write("hello", storage.data_file)
        
        assert storage.data_file.read_text() == "hello"

    def test_atomic_write_replaces_existing(self, tmp_path):
        storage = JsonStorage(tmp_path / "test.json")
        storage.data_file.write_text("old content")
        
        storage._atomic_write("new content", storage.data_file)
        assert storage.data_file.read_text() == "new content"

    def test_atomic_write_no_temp_files_remain(self, tmp_path):
        storage = JsonStorage(tmp_path / "test.json")
        storage._atomic_write("data", storage.data_file)
        
        remaining = list(tmp_path.glob(".tmp_*"))
        assert len(remaining) == 0
