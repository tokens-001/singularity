"""Tests for TodoService business logic."""

import pytest

from todo.models import ValidationError
from todo.service import TodoService


class TestServiceAdd:
    """Test service add operations."""

    def test_add_returns_todo(self, service):
        todo = service.add("Buy milk")
        assert todo.title == "Buy milk"
        assert todo.completed is False
        assert todo.id  # Has an ID

    def test_add_persists_todo(self, service):
        service.add("Task 1")
        todos = service.list_all()
        assert len(todos) == 1
        assert todos[0].title == "Task 1"

    def test_add_multiple(self, service):
        service.add("Task 1")
        service.add("Task 2")
        service.add("Task 3")
        
        todos = service.list_all()
        assert len(todos) == 3

    def test_add_with_description(self, service):
        todo = service.add("Task", description="Details here")
        assert todo.description == "Details here"

    def test_add_empty_title_raises(self, service):
        with pytest.raises(ValidationError):
            service.add("")


class TestServiceList:
    """Test service list operations."""

    def test_list_empty(self, service):
        assert service.list_all() == []

    def test_list_completed(self, service):
        t1 = service.add("Task 1")
        t2 = service.add("Task 2")
        service.complete(t1.id)
        
        completed = service.list_completed()
        assert len(completed) == 1
        assert completed[0].title == "Task 1"

    def test_list_pending(self, service):
        t1 = service.add("Task 1")
        t2 = service.add("Task 2")
        service.complete(t1.id)
        
        pending = service.list_pending()
        assert len(pending) == 1
        assert pending[0].title == "Task 2"


class TestServiceGet:
    """Test service get operations."""

    def test_get_existing(self, service):
        added = service.add("Task")
        found = service.get(added.id)
        assert found is not None
        assert found.title == "Task"

    def test_get_nonexistent(self, service):
        assert service.get("nonexistent-id") is None


class TestServiceComplete:
    """Test service complete/uncomplete operations."""

    def test_complete_existing(self, service):
        todo = service.add("Task")
        result = service.complete(todo.id)
        assert result is True
        
        found = service.get(todo.id)
        assert found.completed is True

    def test_complete_nonexistent(self, service):
        result = service.complete("nonexistent")
        assert result is False

    def test_uncomplete(self, service):
        todo = service.add("Task")
        service.complete(todo.id)
        service.uncomplete(todo.id)
        
        found = service.get(todo.id)
        assert found.completed is False


class TestServiceUpdate:
    """Test service update operations."""

    def test_update_title(self, service):
        todo = service.add("Original")
        service.update(todo.id, title="Updated")
        
        found = service.get(todo.id)
        assert found.title == "Updated"

    def test_update_description(self, service):
        todo = service.add("Task")
        service.update(todo.id, description="New desc")
        
        found = service.get(todo.id)
        assert found.description == "New desc"

    def test_update_nonexistent(self, service):
        result = service.update("nonexistent", title="X")
        assert result is False

    def test_update_with_invalid_title_raises(self, service):
        todo = service.add("Valid")
        with pytest.raises(ValidationError):
            service.update(todo.id, title="")


class TestServiceDelete:
    """Test service delete operations."""

    def test_delete_existing(self, service):
        todo = service.add("Task")
        result = service.delete(todo.id)
        assert result is True
        assert service.list_all() == []

    def test_delete_nonexistent(self, service):
        result = service.delete("nonexistent")
        assert result is False

    def test_clear_all(self, service):
        service.add("Task 1")
        service.add("Task 2")
        service.add("Task 3")
        
        count = service.delete_all()
        assert count == 3
        assert service.list_all() == []
