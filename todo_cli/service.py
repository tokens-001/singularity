"""TodoService — CRUD layer over Storage."""
import uuid
from typing import List, Dict, Any, Optional

from .storage import Storage


class TodoService:
    def __init__(self, storage: Storage):
        self._storage = storage
        self._todos: List[Dict[str, Any]] = []

    def load(self) -> None:
        self._todos = self._storage.load()

    def _save(self) -> None:
        self._storage.save(self._todos)

    def add(self, title: str, description: str = "") -> Dict[str, Any]:
        todo = {
            "id": uuid.uuid4().hex,
            "title": title,
            "description": description,
            "status": "pending",
        }
        self._todos.append(todo)
        self._save()
        return dict(todo)

    def bulk_add(self, items: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        created = []
        for item in items:
            todo = {
                "id": uuid.uuid4().hex,
                "title": item.get("title", ""),
                "description": item.get("description", ""),
                "status": "pending",
            }
            self._todos.append(todo)
            created.append(dict(todo))
        self._save()
        return created

    def list_all(self, status: str = "") -> List[Dict[str, Any]]:
        if status:
            return [dict(t) for t in self._todos if t.get("status") == status]
        return [dict(t) for t in self._todos]

    def get(self, todo_id: str) -> Optional[Dict[str, Any]]:
        for t in self._todos:
            if t["id"] == todo_id:
                return dict(t)
        return None

    def update(self, todo_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        for t in self._todos:
            if t["id"] == todo_id:
                t.update(kwargs)
                self._save()
                return dict(t)
        return None

    def delete(self, todo_id: str) -> bool:
        for i, t in enumerate(self._todos):
            if t["id"] == todo_id:
                self._todos.pop(i)
                self._save()
                return True
        return False

    def count(self) -> int:
        return len(self._todos)
