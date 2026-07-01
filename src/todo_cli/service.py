"""
Business logic layer for TODO management.

This layer knows about todos (id, title, status, etc.) but does NOT
know about files or storage — it delegates persistence to Storage.
"""

import time
import uuid
from typing import Any

from .storage import Storage


class TodoService:
    """Manages TODO items with CRUD operations."""

    def __init__(self, storage: Storage):
        self._storage = storage
        self._data: list[dict[str, Any]] = []

    def load(self) -> None:
        """Load todos from storage."""
        self._data = self._storage.load()

    def save(self) -> None:
        """Persist current todos to storage."""
        self._storage.save(self._data)

    def add(self, title: str, description: str = "") -> dict[str, Any]:
        """Add a new todo item.

        Args:
            title: Todo title (required)
            description: Optional description

        Returns:
            The created todo dict
        """
        todo = {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "description": description,
            "status": "pending",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._data.append(todo)
        self.save()
        return todo

    def delete(self, todo_id: str) -> bool:
        """Delete a todo by ID.

        Returns:
            True if deleted, False if not found
        """
        original_len = len(self._data)
        self._data = [t for t in self._data if t["id"] != todo_id]
        if len(self._data) < original_len:
            self.save()
            return True
        return False

    def update(self, todo_id: str, **kwargs: Any) -> dict[str, Any] | None:
        """Update a todo's fields.

        Args:
            todo_id: ID of the todo to update
            **kwargs: Fields to update (title, description, status)

        Returns:
            Updated todo dict, or None if not found
        """
        allowed_fields = {"title", "description", "status"}
        for key in kwargs:
            if key not in allowed_fields:
                raise ValueError(f"Cannot update field: {key}")

        for todo in self._data:
            if todo["id"] == todo_id:
                for key, value in kwargs.items():
                    todo[key] = value
                todo["updated_at"] = time.time()
                self.save()
                return todo
        return None

    def get(self, todo_id: str) -> dict[str, Any] | None:
        """Get a single todo by ID."""
        for todo in self._data:
            if todo["id"] == todo_id:
                return todo
        return None

    def list_all(self, status: str | None = None) -> list[dict[str, Any]]:
        """List all todos, optionally filtered by status.

        Args:
            status: If provided, only return todos with this status

        Returns:
            List of todo dicts
        """
        if status is None:
            return list(self._data)
        return [t for t in self._data if t["status"] == status]

    def count(self) -> int:
        """Return total number of todos."""
        return len(self._data)

    def bulk_add(self, items: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Add multiple todos at once.

        Args:
            items: List of dicts with 'title' and optional 'description'

        Returns:
            List of created todo dicts
        """
        created = []
        now = time.time()
        for item in items:
            todo = {
                "id": uuid.uuid4().hex[:8],
                "title": item["title"],
                "description": item.get("description", ""),
                "status": "pending",
                "created_at": now,
                "updated_at": now,
            }
            self._data.append(todo)
            created.append(todo)
        self.save()
        return created
