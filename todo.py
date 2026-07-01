"""核心模型层：Todo 实体与业务规则。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


MAX_TITLE_LENGTH = 200


class ValidationError(Exception):
    pass


@dataclass
class Todo:
    id: str
    title: str
    description: str = ""
    done: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def validate(self) -> None:
        if not self.title or not self.title.strip():
            raise ValidationError("title cannot be empty")
        if len(self.title) > MAX_TITLE_LENGTH:
            raise ValidationError(f"title exceeds max length {MAX_TITLE_LENGTH}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "done": self.done,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Todo":
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            done=data.get("done", False),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )
