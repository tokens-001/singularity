"""Observer per-session message history cache.

Persists the most recent ``N`` inbound/outbound messages keyed by
session identifier.  Designed for:

* fast replay of recent context after a client reconnects
* broadcast history to newly connected WebSocket clients
* optional durable persistence so context survives server restarts

All operations are asyncio-safe; the cache itself stores plain Python
objects and leaves serialisation details to callers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from observer.config import MAX_HISTORY_LENGTH

logger = logging.getLogger("observer.history")


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class HistoryEntry:
    """A single recorded message together with its metadata."""

    ts: float
    direction: str  # "in" for client -> server, "out" for server -> client
    message: dict[str, Any]
    channel: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "direction": self.direction,
            "message": self.message,
            "channel": self.channel,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryEntry":
        return cls(
            ts=float(data["ts"]),
            direction=data["direction"],
            message=data["message"],
            channel=data.get("channel"),
        )


# --------------------------------------------------------------------------- #
# Per-session history buffer
# --------------------------------------------------------------------------- #
class SessionHistory:
    """Ring buffer holding the latest messages for one session."""

    def __init__(self, session_id: str, max_len: int = MAX_HISTORY_LENGTH) -> None:
        self.session_id = session_id
        self._max_len = max_len
        self._entries: deque[HistoryEntry] = deque(maxlen=max_len)
        self._lock = asyncio.Lock()

    @property
    def count(self) -> int:
        return len(self._entries)

    async def append(self, entry: HistoryEntry) -> None:
        """Append a new entry; drops the oldest when over capacity."""
        async with self._lock:
            self._entries.append(entry)

    async def extend(self, entries: list[HistoryEntry]) -> None:
        """Append multiple entries atomically."""
        async with self._lock:
            for entry in entries:
                self._entries.append(entry)

    async def recent(
        self,
        limit: int | None = None,
        direction: str | None = None,
        event_type: str | None = None,
    ) -> list[HistoryEntry]:
        """Return recent entries, optionally filtered.

        Args:
            limit: Maximum number of entries to return (default: all).
            direction: Filter by ``"in"`` or ``"out"``.
            event_type: Filter by ``message["event"]`` value.
        """
        async with self._lock:
            entries = list(self._entries)

        if direction:
            entries = [e for e in entries if e.direction == direction]
        if event_type:
            entries = [
                e
                for e in entries
                if isinstance(e.message, dict) and e.message.get("event") == event_type
            ]
        if limit is not None and limit >= 0:
            entries = entries[-limit:]
        return entries

    async def snapshot(self) -> list[dict[str, Any]]:
        """Return a JSON-safe list of all entries."""
        async with self._lock:
            return [e.to_dict() for e in self._entries]

    async def restore(self, data: list[dict[str, Any]]) -> None:
        """Replace the current buffer with previously serialised data."""
        async with self._lock:
            self._entries.clear()
            for item in data:
                self._entries.append(HistoryEntry.from_dict(item))

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()


# --------------------------------------------------------------------------- #
# Global history cache
# --------------------------------------------------------------------------- #
class HistoryCache:
    """In-memory cache of ``session_id -> SessionHistory``.

    Supports persistence to disk as newline-delimited JSON so that recent
    context can survive a server restart.  The caller decides when to
    ``save()`` / ``load()``; by default the cache is transient.
    """

    def __init__(
        self,
        max_len: int = MAX_HISTORY_LENGTH,
        persistence_path: str | Path | None = None,
    ) -> None:
        self._max_len = max_len
        self._persistence_path = Path(persistence_path) if persistence_path else None
        self._sessions: dict[str, SessionHistory] = {}
        self._lock = asyncio.Lock()

    # ── session lifecycle ──────────────────────────────────────────────────
    async def get_or_create(self, session_id: str) -> SessionHistory:
        """Return existing history or create a new ring buffer."""
        async with self._lock:
            history = self._sessions.get(session_id)
            if history is None:
                history = SessionHistory(session_id, max_len=self._max_len)
                self._sessions[session_id] = history
                logger.debug("Created history buffer for session %s", session_id)
            return history

    async def get(self, session_id: str) -> SessionHistory | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def remove(self, session_id: str) -> SessionHistory | None:
        """Remove and return a session's history, if any."""
        async with self._lock:
            history = self._sessions.pop(session_id, None)
            if history:
                logger.debug("Removed history buffer for session %s", session_id)
            return history

    async def all_session_ids(self) -> list[str]:
        async with self._lock:
            return list(self._sessions.keys())

    @property
    async def count(self) -> int:
        async with self._lock:
            return len(self._sessions)

    # ── recording helpers ──────────────────────────────────────────────────
    async def record_inbound(
        self,
        session_id: str,
        message: dict[str, Any],
        ts: float | None = None,
    ) -> None:
        """Record a message received from the client."""
        history = await self.get_or_create(session_id)
        await history.append(
            HistoryEntry(
                ts=ts or asyncio.get_event_loop().time(),
                direction="in",
                message=message,
            )
        )

    async def record_outbound(
        self,
        session_id: str,
        message: dict[str, Any],
        channel: str | None = None,
        ts: float | None = None,
    ) -> None:
        """Record a message sent to the client."""
        history = await self.get_or_create(session_id)
        await history.append(
            HistoryEntry(
                ts=ts or asyncio.get_event_loop().time(),
                direction="out",
                message=message,
                channel=channel,
            )
        )

    async def replay(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return the most recent messages for a session as plain dicts."""
        history = await self.get(session_id)
        if history is None:
            return []
        entries = await history.recent(limit=limit)
        return [e.to_dict() for e in entries]

    # ── persistence ────────────────────────────────────────────────────────
    async def save(self) -> None:
        """Persist current cache to ``persistence_path``."""
        if self._persistence_path is None:
            return

        async with self._lock:
            snapshot = {
                sid: [e.to_dict() for e in hist._entries]
                for sid, hist in self._sessions.items()
            }

        self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._persistence_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            ),
        )
        logger.info("Saved history cache to %s", self._persistence_path)

    async def load(self) -> int:
        """Load cache from ``persistence_path``. Returns number of sessions restored."""
        if self._persistence_path is None or not self._persistence_path.exists():
            return 0

        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(
            None,
            lambda: self._persistence_path.read_text(encoding="utf-8"),
        )
        if not text.strip():
            return 0

        data: dict[str, list[dict[str, Any]]] = json.loads(text)
        restored = 0
        async with self._lock:
            for sid, entries in data.items():
                history = SessionHistory(sid, max_len=self._max_len)
                await history.restore(entries)
                self._sessions[sid] = history
                restored += 1
        logger.info("Loaded %d session histories from %s", restored, self._persistence_path)
        return restored

    async def clear_all(self) -> None:
        async with self._lock:
            self._sessions.clear()
        if self._persistence_path and self._persistence_path.exists():
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._persistence_path.unlink)


# --------------------------------------------------------------------------- #
# Singleton accessor
# --------------------------------------------------------------------------- #
_cache: HistoryCache | None = None


def get_history_cache(
    max_len: int = MAX_HISTORY_LENGTH,
    persistence_path: str | Path | None = None,
) -> HistoryCache:
    """Return the global ``HistoryCache`` singleton."""
    global _cache
    if _cache is None:
        _cache = HistoryCache(max_len=max_len, persistence_path=persistence_path)
    return _cache


def reset_history_cache() -> None:
    """Reset the global singleton, primarily for tests."""
    global _cache
    _cache = None
