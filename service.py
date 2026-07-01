"""业务服务层：编排模型与持久化，提供清晰的接口契约。"""

import uuid
from typing import Any

from todo import Todo, ValidationError
from storage import Storage


class TodoService:
    """业务服务接口：
    - list_todos() -> list[Todo]
    - create_todo(title, description="") -> Todo
    - update_todo(todo_id, **changes) -> Todo
    - delete_todo(todo_id) -> bool
    """

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def _load(self) -> list[Todo]:
        raw = self._storage.load()
        return [Todo.from_dict(item) for item in raw]

    def _save(self, todos: list[Todo]) -> None:
        self._storage.save([t.to_dict() for t in todos])

    def list_todos(self) -> list[Todo]:
        return self._load()

    def create_todo(self, title: str, description: str = "") -> Todo:
        todo = Todo(id=str(uuid.uuid4()), title=title, description=description)
        todo.validate()
        todos = self._load()
        todos.append(todo)
        self._save(todos)
        return todo

    def update_todo(self, todo_id: str, **changes: Any) -> Todo:
        todos = self._load()
        for t in todos:
            if t.id == todo_id:
                for key, value in changes.items():
                    if hasattr(t, key):
                        setattr(t, key, value)
                t.validate()
                self._save(todos)
                return t
        raise ValueError(f"todo {todo_id} not found")

    def delete_todo(self, todo_id: str) -> bool:
        todos = self._load()
        original_len = len(todos)
        todos = [t for t in todos if t.id != todo_id]
        if len(todos) == original_len:
            return False
        self._save(todos)
        return True
