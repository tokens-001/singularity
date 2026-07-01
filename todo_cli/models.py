"""Domain models for the Todo CLI.

This module is the single source of truth for what a Todo is.
No file I/O, no CLI concerns — just data + validation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

MAX_TITLE_LENGTH = 200
MIN_TITLE_LENGTH = 1


class ValidationError(ValueError):
    """Raised when a Todo fails validation."""

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


class Todo:
    """A single todo item.

    Attributes:
        id: Unique identifier (UUID4 hex string).
        title: Human-readable title (1–200 chars).
        description: Optional longer description.
        completed: Whether the task is done.
        priority: 1 (high) | 2 (medium) | 3 (low).
        created_at: ISO-8601 UTC timestamp string.
        updated_at: ISO-8601 UTC timestamp string.
    """

    __slots__ = ("id", "title", "description", "completed", "priority", "created_at", "updated_at")

    VALID_PRIORITIES = {1, 2, 3}

    # ------------------------------------------------------------------ #
    #  Construction
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        id: str | None = None,
        title: str = "",
        description: str = "",
        completed: bool = False,
        priority: int = 2,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        self.id = id if id else uuid.uuid4().hex
        self.title = title
        self.description = description
        self.completed = completed
        self.priority = priority
        self.created_at = created_at or now
        self.updated_at = updated_at or now

    # ------------------------------------------------------------------ #
    #  Validation
    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        """Validate all fields. Raise ``ValidationError`` on the first problem.

        Checks:
            * title is non-empty and ≤ ``MAX_TITLE_LENGTH`` characters.
            * title has no control characters (ASCII < 0x20 except tab/newline).
            * priority is in ``{1, 2, 3}``.
            * id is a non-empty string.
            * timestamps are non-empty strings.
            * description length ≤ 2000 characters.
        """
        # --- title -------------------------------------------------------
        if not isinstance(self.title, str) or len(self.title.strip()) < MIN_TITLE_LENGTH:
            raise ValidationError("title", "must not be empty or whitespace-only")

        if len(self.title) > MAX_TITLE_LENGTH:
            raise ValidationError(
                "title", f"exceeds maximum length {MAX_TITLE_LENGTH} (got {len(self.title)})"
            )

        if any(ord(ch) < 0x20 and ch not in "\t\n" for ch in self.title):
            raise ValidationError("title", "contains control characters")

        # --- description -------------------------------------------------
        if len(self.description) > 2000:
            raise ValidationError("description", "exceeds maximum length 2000")

        # --- priority ----------------------------------------------------
        if self.priority not in self.VALID_PRIORITIES:
            raise ValidationError(
                "priority",
                f"must be one of {sorted(self.VALID_PRIORITIES)}, got {self.priority!r}",
            )

        # --- id ----------------------------------------------------------
        if not isinstance(self.id, str) or not self.id:
            raise ValidationError("id", "must be a non-empty string")

        # --- timestamps --------------------------------------------------
        for ts_label, ts_val in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if not isinstance(ts_val, str) or not ts_val.strip():
                raise ValidationError(ts_label, "must be a non-empty ISO-8601 string")

    # ------------------------------------------------------------------ #
    #  Serialization
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation of this todo.

        The dict is guaranteed to round-trip through ``Todo.from_dict``.
        """
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "completed": self.completed,
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Todo":
        """Build a ``Todo`` from a plain dict.

        Missing keys fall back to ``__init__`` defaults; extra keys are silently
        ignored so the format can evolve without breaking old data.
        """
        return cls(
            id=data.get("id"),
            title=data.get("title", ""),
            description=data.get("description", ""),
            completed=data.get("completed", False),
            priority=data.get("priority", 2),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

    # ------------------------------------------------------------------ #
    #  Dunder helpers (useful for testing & debugging)
    # ------------------------------------------------------------------ #
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Todo):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"Todo(id={self.id!r}, title={self.title!r}, "
            f"completed={self.completed}, priority={self.priority})"
        )


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
