"""Tests for Todo model: from_dict/to_dict roundtrip and validation."""

import pytest
from datetime import datetime

from todo.models import Todo, ValidationError


class TestTodoValidation:
    """Test Todo.validate() detects invalid states."""

    def test_valid_todo_passes(self):
        todo = Todo(title="Buy groceries")
        todo.validate()  # Should not raise

    def test_empty_title_raises(self):
        todo = Todo(title="")
        with pytest.raises(ValidationError, match="empty"):
            todo.validate()

    def test_whitespace_only_title_raises(self):
        todo = Todo(title="   ")
        with pytest.raises(ValidationError, match="empty"):
            todo.validate()

    def test_title_too_long_raises(self):
        todo = Todo(title="x" * 201)
        with pytest.raises(ValidationError, match="exceed"):
            todo.validate()

    def test_title_at_max_length_passes(self):
        todo = Todo(title="x" * 200)
        todo.validate()  # Should not raise

    def test_description_too_long_raises(self):
        todo = Todo(title="Valid", description="x" * 2001)
        with pytest.raises(ValidationError, match="exceed"):
            todo.validate()

    def test_description_at_max_length_passes(self):
        todo = Todo(title="Valid", description="x" * 2000)
        todo.validate()  # Should not raise

    def test_empty_id_raises(self):
        todo = Todo(title="Valid", id="")
        with pytest.raises(ValidationError, match="ID"):
            todo.validate()


class TestTodoDictRoundtrip:
    """Test Todo.from_dict/to_dict bidirectional conversion."""

    def test_to_dict_contains_all_fields(self):
        todo = Todo(
            id="abc-123",
            title="Test task",
            description="Some details",
            completed=True,
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-02T00:00:00",
        )
        d = todo.to_dict()
        
        assert d["id"] == "abc-123"
        assert d["title"] == "Test task"
        assert d["description"] == "Some details"
        assert d["completed"] is True
        assert d["created_at"] == "2024-01-01T00:00:00"
        assert d["updated_at"] == "2024-01-02T00:00:00"

    def test_from_dict_creates_correct_instance(self):
        data = {
            "id": "abc-123",
            "title": "Test task",
            "description": "Some details",
            "completed": True,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-02T00:00:00",
        }
        todo = Todo.from_dict(data)
        
        assert todo.id == "abc-123"
        assert todo.title == "Test task"
        assert todo.description == "Some details"
        assert todo.completed is True
        assert todo.created_at == "2024-01-01T00:00:00"
        assert todo.updated_at == "2024-01-02T00:00:00"

    def test_roundtrip_preserves_data(self):
        original = Todo(
            id="test-id-456",
            title="Roundtrip test",
            description="Testing serialization",
            completed=False,
            created_at="2024-06-15T10:30:00",
            updated_at="2024-06-15T11:00:00",
        )
        
        restored = Todo.from_dict(original.to_dict())
        
        assert restored.id == original.id
        assert restored.title == original.title
        assert restored.description == original.description
        assert restored.completed == original.completed
        assert restored.created_at == original.created_at
        assert restored.updated_at == original.updated_at

    def test_from_dict_defaults_for_optional_fields(self):
        data = {"id": "x", "title": "Minimal"}
        todo = Todo.from_dict(data)
        
        assert todo.description == ""
        assert todo.completed is False

    def test_from_dict_missing_required_field_raises(self):
        with pytest.raises(KeyError):
            Todo.from_dict({"id": "x"})  # missing title

    def test_roundtrip_multiple_todos(self):
        todos = [
            Todo(id="1", title="First", completed=False),
            Todo(id="2", title="Second", completed=True),
            Todo(id="3", title="Third", description="With desc"),
        ]
        
        dicts = [t.to_dict() for t in todos]
        restored = [Todo.from_dict(d) for d in dicts]
        
        for orig, rest in zip(todos, restored):
            assert orig.id == rest.id
            assert orig.title == rest.title
            assert orig.completed == rest.completed


class TestTodoMutations:
    """Test Todo state mutation methods."""

    def test_mark_completed(self):
        todo = Todo(title="Test")
        assert todo.completed is False
        
        todo.mark_completed()
        assert todo.completed is True

    def test_mark_pending(self):
        todo = Todo(title="Test", completed=True)
        
        todo.mark_pending()
        assert todo.completed is False

    def test_update_title(self):
        todo = Todo(title="Original")
        todo.update(title="Updated")
        assert todo.title == "Updated"

    def test_update_description(self):
        todo = Todo(title="Test")
        todo.update(description="New desc")
        assert todo.description == "New desc"

    def test_update_with_invalid_title_raises(self):
        todo = Todo(title="Valid")
        with pytest.raises(ValidationError):
            todo.update(title="")
