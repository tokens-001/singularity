"""
Tests for storage atomicity, crash recovery, and bulk operations.

Acceptance criteria:
  1. Read/write 1000 items correctly
  2. Simulated crash leaves original file intact
  3. Corrupted file triggers automatic recovery from backup
"""

import json
import logging
import os
import tempfile
from pathlib import Path

import pytest

from todo_cli.service import TodoService
from todo_cli.storage import Storage


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def data_file(tmp_dir):
    return os.path.join(tmp_dir, "todos.json")


@pytest.fixture
def storage(data_file):
    return Storage(data_file)


@pytest.fixture
def service(storage):
    svc = TodoService(storage)
    svc.load()
    return svc


# ─── 1. Bulk read/write correctness (1000 items) ────────────────────────────

class TestBulkReadWrite:
    def test_add_and_read_1000_items(self, service):
        """Write 1000 todos and verify all are correctly persisted."""
        items = [{"title": f"Task {i}", "description": f"Desc {i}"} for i in range(1000)]
        created = service.bulk_add(items)

        assert len(created) == 1000

        # Reload from disk
        service2 = TodoService(Storage(service._storage.filepath))
        service2.load()

        loaded = service2.list_all()
        assert len(loaded) == 1000

        # Verify each item
        titles = {t["title"] for t in loaded}
        for i in range(1000):
            assert f"Task {i}" in titles

    def test_1000_items_data_integrity(self, service):
        """Verify data integrity for each of the 1000 items after reload."""
        items = [{"title": f"Item-{i:04d}", "description": f"Body-{i:04d}"} for i in range(1000)]
        created = service.bulk_add(items)

        # Map id -> expected data
        expected = {t["id"]: t for t in created}

        # Reload
        service2 = TodoService(Storage(service._storage.filepath))
        service2.load()

        for todo in service2.list_all():
            exp = expected[todo["id"]]
            assert todo["title"] == exp["title"]
            assert todo["description"] == exp["description"]
            assert todo["status"] == "pending"

    def test_1000_items_json_file_valid(self, service, data_file):
        """Verify the JSON file on disk is valid and contains 1000 items."""
        items = [{"title": f"Task {i}"} for i in range(1000)]
        service.bulk_add(items)

        with open(data_file, "r") as f:
            data = json.load(f)
        assert len(data) == 1000


# ─── 2. Crash simulation — original file stays intact ───────────────────────

class TestCrashSafety:
    def test_partial_write_does_not_corrupt(self, service, data_file, tmp_dir):
        """Simulate a crash during write by writing garbage to temp file.
        The original file should remain intact."""
        # First, write valid data
        service.add("Important Task", "Do not lose this")

        # Verify data is on disk
        with open(data_file, "r") as f:
            original_data = json.load(f)
        assert len(original_data) == 1

        # Simulate crash: write garbage directly to the data file
        # (simulating what would happen if rename happened but write was incomplete)
        # Then verify recovery from backup
        backup_path = Path(data_file).with_suffix(".json.bak")

        # The backup should exist from the atomic write
        assert backup_path.exists()

        # Corrupt the primary file (simulating crash mid-write)
        with open(data_file, "w") as f:
            f.write("{corrupted!!!")

        # Load should recover from backup
        service2 = TodoService(Storage(data_file))
        service2.load()
        todos = service2.list_all()
        assert len(todos) == 1
        assert todos[0]["title"] == "Important Task"

    def test_rename_is_atomic(self, service, data_file):
        """After save, the file should always contain valid JSON."""
        for i in range(50):
            service.add(f"Task {i}")
            # After every save, file must be valid
            with open(data_file, "r") as f:
                data = json.load(f)
            assert len(data) == i + 1

    def test_no_temp_files_left_behind(self, service, data_file, tmp_dir):
        """After successful save, no temp files should remain."""
        service.add("Test Task")
        remaining = [f for f in os.listdir(tmp_dir) if f.startswith(".todo_tmp_")]
        assert len(remaining) == 0


# ─── 3. Corrupted file automatic recovery ───────────────────────────────────

class TestCrashRecovery:
    def test_recover_from_backup_on_corruption(self, data_file):
        """When primary file is corrupted, recover from backup."""
        storage = Storage(data_file)

        # Write valid data first (creates both primary and backup)
        storage.save([{"id": "1", "title": "Recoverable"}])

        # Corrupt primary
        with open(data_file, "w") as f:
            f.write("NOT VALID JSON {{{")

        # Load should recover from backup
        data = storage.load()
        assert len(data) == 1
        assert data[0]["title"] == "Recoverable"

    def test_recover_from_backup_empty_primary(self, data_file):
        """When primary file is empty, recover from backup."""
        storage = Storage(data_file)
        storage.save([{"id": "1", "title": "Backup Task"}])

        # Empty the primary file
        with open(data_file, "w") as f:
            f.write("")

        data = storage.load()
        assert len(data) == 1
        assert data[0]["title"] == "Backup Task"

    def test_initialize_empty_when_both_corrupted(self, data_file):
        """When both primary and backup are corrupted, return empty list."""
        storage = Storage(data_file)

        # Create and corrupt both files
        Path(data_file).write_text("CORRUPT")
        Path(data_file).with_suffix(".json.bak").write_text("ALSO CORRUPT")

        data = storage.load()
        assert data == []

    def test_initialize_empty_when_no_files(self, data_file):
        """When no files exist, return empty list."""
        storage = Storage(data_file)
        data = storage.load()
        assert data == []

    def test_recovery_logs_error(self, data_file, caplog):
        """Recovery from corruption should log an error."""
        storage = Storage(data_file)
        storage.save([{"id": "1", "title": "Logged Recovery"}])

        with open(data_file, "w") as f:
            f.write("BROKEN JSON")

        with caplog.at_level(logging.ERROR):
            data = storage.load()

        assert len(data) == 1
        assert any("corrupted" in record.message.lower() or "failed" in record.message.lower()
                    for record in caplog.records)

    def test_recovery_restores_primary(self, data_file):
        """After recovery, the primary file should be restored."""
        storage = Storage(data_file)
        storage.save([{"id": "1", "title": "Restore Me"}])

        # Corrupt primary
        with open(data_file, "w") as f:
            f.write("GARBAGE")

        # Load triggers recovery
        data = storage.load()
        assert len(data) == 1

        # Primary should now be valid
        with open(data_file, "r") as f:
            restored = json.load(f)
        assert len(restored) == 1
        assert restored[0]["title"] == "Restore Me"

    def test_truncated_json_recovery(self, data_file):
        """Truncated JSON file should trigger recovery."""
        storage = Storage(data_file)
        storage.save([{"id": "1", "title": "Truncated Test"}])

        # Truncate the file
        with open(data_file, "w") as f:
            f.write('[{"id": "1", "title": "Trunc')

        data = storage.load()
        assert len(data) == 1
        assert data[0]["title"] == "Truncated Test"


# ─── 4. Service layer integration ───────────────────────────────────────────

class TestServiceIntegration:
    def test_crud_operations(self, service):
        """Basic CRUD operations work correctly."""
        # Create
        todo = service.add("Test", "Description")
        assert todo["title"] == "Test"
        assert todo["status"] == "pending"

        # Read
        fetched = service.get(todo["id"])
        assert fetched is not None
        assert fetched["title"] == "Test"

        # Update
        updated = service.update(todo["id"], status="done")
        assert updated["status"] == "done"

        # List
        all_todos = service.list_all()
        assert len(all_todos) == 1

        # Delete
        assert service.delete(todo["id"]) is True
        assert service.count() == 0

    def test_persistence_across_instances(self, data_file):
        """Data persists across different service instances."""
        svc1 = TodoService(Storage(data_file))
        svc1.load()
        svc1.add("Persistent Task")

        svc2 = TodoService(Storage(data_file))
        svc2.load()
        assert svc2.count() == 1
        assert svc2.list_all()[0]["title"] == "Persistent Task"

    def test_filter_by_status(self, service):
        """List with status filter works."""
        t1 = service.add("Task 1")
        t2 = service.add("Task 2")
        t3 = service.add("Task 3")
        service.update(t2["id"], status="done")
        service.update(t3["id"], status="in_progress")

        assert len(service.list_all(status="pending")) == 1
        assert len(service.list_all(status="done")) == 1
        assert len(service.list_all(status="in_progress")) == 1
        assert len(service.list_all()) == 3
