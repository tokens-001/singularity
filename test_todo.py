#!/usr/bin/env python3
"""Test suite for TODO tool - validates atomic writes and crash recovery."""
import json
import os
import tempfile
import unittest
from storage import Storage
from manager import TodoManager


class TestStorage(unittest.TestCase):
    """Test storage layer atomic writes and recovery."""
    
    def setUp(self):
        """Create temporary directory for tests."""
        self.test_dir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.test_dir, 'test_todos.json')
        self.storage = Storage(self.test_file)
    
    def tearDown(self):
        """Clean up temporary files."""
        for filename in os.listdir(self.test_dir):
            os.remove(os.path.join(self.test_dir, filename))
        os.rmdir(self.test_dir)
    
    def test_save_and_load(self):
        """Test basic save and load functionality."""
        data = [{'id': 1, 'title': 'Test', 'completed': False}]
        self.storage.save(data)
        loaded = self.storage.load()
        self.assertEqual(loaded, data)
    
    def test_1000_items(self):
        """Test handling 1000 items correctly."""
        data = [{'id': i, 'title': f'Task {i}', 'completed': i % 2 == 0} for i in range(1000)]
        self.storage.save(data)
        loaded = self.storage.load()
        self.assertEqual(len(loaded), 1000)
        self.assertEqual(loaded, data)
    
    def test_atomic_write_creates_temp_file(self):
        """Test that atomic write strategy uses temp file."""
        data = [{'id': 1, 'title': 'Test', 'completed': False}]
        self.storage.save(data)
        # Temp file should not exist after successful save
        self.assertFalse(os.path.exists(self.storage.temp_path))
        # Main file should exist
        self.assertTrue(os.path.exists(self.test_file))
    
    def test_backup_created(self):
        """Test that backup is created on subsequent saves."""
        data1 = [{'id': 1, 'title': 'First', 'completed': False}]
        data2 = [{'id': 1, 'title': 'Second', 'completed': False}]
        
        self.storage.save(data1)
        self.storage.save(data2)
        
        # Backup should exist
        self.assertTrue(os.path.exists(self.storage.backup_path))
        
        # Backup should contain first version
        with open(self.storage.backup_path, 'r') as f:
            backup_data = json.load(f)
        self.assertEqual(backup_data, data1)
    
    def test_crash_recovery_from_backup(self):
        """Test recovery from backup when main file is corrupted."""
        # Save valid data
        original_data = [{'id': 1, 'title': 'Original', 'completed': False}]
        self.storage.save(original_data)
        
        # Save again to create backup
        new_data = [{'id': 2, 'title': 'New', 'completed': True}]
        self.storage.save(new_data)
        
        # Corrupt main file
        with open(self.test_file, 'w') as f:
            f.write('INVALID JSON{{{')
        
        # Load should recover from backup
        loaded = self.storage.load()
        self.assertEqual(loaded, original_data)
    
    def test_empty_init_on_total_corruption(self):
        """Test initialization with empty data when both files are corrupted."""
        # Corrupt main file
        with open(self.test_file, 'w') as f:
            f.write('CORRUPTED')
        
        # No backup exists
        loaded = self.storage.load()
        self.assertEqual(loaded, [])
    
    def test_file_not_found(self):
        """Test handling of missing file."""
        loaded = self.storage.load()
        self.assertEqual(loaded, [])


class TestTodoManager(unittest.TestCase):
    """Test business logic layer."""
    
    def setUp(self):
        """Create temporary storage for tests."""
        self.test_dir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.test_dir, 'test_todos.json')
        self.storage = Storage(self.test_file)
        self.manager = TodoManager(self.storage)
    
    def tearDown(self):
        """Clean up temporary files."""
        for filename in os.listdir(self.test_dir):
            os.remove(os.path.join(self.test_dir, filename))
        os.rmdir(self.test_dir)
    
    def test_add_item(self):
        """Test adding a TODO item."""
        item = self.manager.add('Test Task')
        self.assertEqual(item['title'], 'Test Task')
        self.assertEqual(item['completed'], False)
        self.assertEqual(len(self.manager.list_all()), 1)
    
    def test_list_items(self):
        """Test listing all items."""
        self.manager.add('Task 1')
        self.manager.add('Task 2')
        items = self.manager.list_all()
        self.assertEqual(len(items), 2)
    
    def test_update_item(self):
        """Test updating a TODO item."""
        item = self.manager.add('Original')
        updated = self.manager.update(item['id'], title='Updated', completed=True)
        self.assertEqual(updated['title'], 'Updated')
        self.assertEqual(updated['completed'], True)
    
    def test_delete_item(self):
        """Test deleting a TODO item."""
        item = self.manager.add('To Delete')
        result = self.manager.delete(item['id'])
        self.assertTrue(result)
        self.assertEqual(len(self.manager.list_all()), 0)
    
    def test_toggle_item(self):
        """Test toggling completion status."""
        item = self.manager.add('Toggle Me')
        self.assertFalse(item['completed'])
        
        toggled = self.manager.toggle(item['id'])
        self.assertTrue(toggled['completed'])
        
        toggled_again = self.manager.toggle(item['id'])
        self.assertFalse(toggled_again['completed'])
    
    def test_get_nonexistent_item(self):
        """Test getting a non-existent item."""
        item = self.manager.get(999)
        self.assertIsNone(item)
    
    def test_1000_operations(self):
        """Test 1000 add operations."""
        for i in range(1000):
            self.manager.add(f'Task {i}')
        
        items = self.manager.list_all()
        self.assertEqual(len(items), 1000)
        
        # Verify persistence
        new_manager = TodoManager(self.storage)
        self.assertEqual(len(new_manager.list_all()), 1000)


class TestCrashSimulation(unittest.TestCase):
    """Test crash scenarios and recovery."""
    
    def setUp(self):
        """Create temporary storage for tests."""
        self.test_dir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.test_dir, 'test_todos.json')
        self.storage = Storage(self.test_file)
    
    def tearDown(self):
        """Clean up temporary files."""
        for filename in os.listdir(self.test_dir):
            os.remove(os.path.join(self.test_dir, filename))
        os.rmdir(self.test_dir)
    
    def test_partial_write_simulation(self):
        """Simulate crash during write by leaving temp file."""
        # Create temp file with partial data
        with open(self.storage.temp_path, 'w') as f:
            f.write('{"incomplete": ')
        
        # Main file should not exist or be unaffected
        manager = TodoManager(self.storage)
        self.assertEqual(len(manager.list_all()), 0)
        
        # Temp file should still exist (not cleaned up in this implementation)
        # but shouldn't affect main operations
    
    def test_backup_integrity_after_crash(self):
        """Test that backup remains intact after simulated crash."""
        # Save initial data
        original = [{'id': 1, 'title': 'Important', 'completed': False}]
        self.storage.save(original)
        
        # Save again to create backup
        updated = [{'id': 1, 'title': 'Updated', 'completed': True}]
        self.storage.save(updated)
        
        # Simulate crash by corrupting main file mid-write
        with open(self.test_file, 'w') as f:
            f.write('{"crashed": true')  # Incomplete JSON
        
        # Recovery should use backup
        recovered = self.storage.load()
        self.assertEqual(recovered, original)
        
        # Backup should still be readable
        with open(self.storage.backup_path, 'r') as f:
            backup = json.load(f)
        self.assertEqual(backup, original)


if __name__ == '__main__':
    unittest.main()
