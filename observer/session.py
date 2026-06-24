"""Observer session manager — 维护 client_id 与上下文、历史消息、待确认动作的映射。

支持多客户端并发，提供线程/协程安全的读写接口，供 observer.server 与外部
调度循环共享同一份会话状态。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from observer.config import (
    HEARTBEAT_INTERVAL_SECONDS,
    MAX_HISTORY_LENGTH,
)

logger = logging.getLogger("observer.session")


# ── 会话上下文 ────────────────────────────────────────────
@dataclass
class SessionContext:
    """单个客户端会话维护的全部状态。"""

    client_id: str
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    subscriptions: set[str] = field(default_factory=set)
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_HISTORY_LENGTH))
    pending_confirmations: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        """更新最后活跃时间。"""
        self.last_seen = time.time()

    def add_message(self, message: dict[str, Any]) -> None:
        """记录一条入站或出站消息。"""
        self.touch()
        entry = {"ts": time.time(), "message": message}
        self.history.append(entry)

    def add_pending(self, action_id: str, action: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """注册一个待客户端确认的动作。

        Args:
            action_id: 动作唯一标识。
            action: 动作内容，会被原样保存。
            timeout: 可选的超时时间（秒），未指定则使用默认心跳超时的 2 倍。

        Returns:
            包含 action_id、created_at、expires_at 的字典。
        """
        self.touch()
        now = time.time()
        expires_at = now + (timeout or HEARTBEAT_INTERVAL_SECONDS * 2)
        payload = {
            "action_id": action_id,
            "action": action,
            "created_at": now,
            "expires_at": expires_at,
        }
        self.pending_confirmations[action_id] = payload
        return payload

    def confirm(self, action_id: str, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """客户端确认动作，返回动作负载或 None（不存在/已过期）。"""
        self.touch()
        pending = self.pending_confirmations.pop(action_id, None)
        if pending is None:
            return None
        if time.time() > pending["expires_at"]:
            return None
        if result is not None:
            pending["result"] = result
            pending["confirmed_at"] = time.time()
        return pending

    def expire_pending(self) -> list[dict[str, Any]]:
        """清理已过期动作，返回被清理的列表。"""
        now = time.time()
        expired = [
            payload
            for action_id, payload in list(self.pending_confirmations.items())
            if now > payload["expires_at"]
        ]
        for payload in expired:
            self.pending_confirmations.pop(payload["action_id"], None)
        return expired


# ── 会话仓库 ──────────────────────────────────────────────
class SessionStore:
    """维护 client_id → SessionContext 的线程/协程安全映射。"""

    def __init__(self, max_history: int = MAX_HISTORY_LENGTH) -> None:
        self._sessions: dict[str, SessionContext] = {}
        self._max_history = max_history
        self._lock = asyncio.Lock()

    # ── 基本 CRUD ─────────────────────────────────────────
    async def create(self, client_id: str, metadata: dict[str, Any] | None = None) -> SessionContext:
        """创建新会话；若已存在则覆盖（断线重连场景）。"""
        async with self._lock:
            ctx = SessionContext(
                client_id=client_id,
                metadata=metadata or {},
                history=deque(maxlen=self._max_history),
            )
            self._sessions[client_id] = ctx
            logger.info("会话创建: %s (总计 %d)", client_id, len(self._sessions))
            return ctx

    async def get(self, client_id: str) -> SessionContext | None:
        """获取会话上下文（线程安全读）。"""
        async with self._lock:
            return self._sessions.get(client_id)

    async def remove(self, client_id: str) -> SessionContext | None:
        """移除会话，返回被移除的上下文或 None。"""
        async with self._lock:
            ctx = self._sessions.pop(client_id, None)
            if ctx:
                logger.info("会话移除: %s (总计 %d)", client_id, len(self._sessions))
            return ctx

    async def exists(self, client_id: str) -> bool:
        async with self._lock:
            return client_id in self._sessions

    @property
    async def count(self) -> int:
        async with self._lock:
            return len(self._sessions)

    async def all_client_ids(self) -> list[str]:
        async with self._lock:
            return list(self._sessions.keys())

    # ── 消息历史 ──────────────────────────────────────────
    async def record_message(self, client_id: str, message: dict[str, Any]) -> bool:
        """记录消息到对应会话历史。"""
        ctx = await self.get(client_id)
        if ctx is None:
            return False
        async with ctx.lock:
            ctx.add_message(message)
        return True

    async def get_history(
        self,
        client_id: str,
        limit: int | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """获取会话历史消息，可选按事件类型过滤。"""
        ctx = await self.get(client_id)
        if ctx is None:
            return []
        async with ctx.lock:
            history = list(ctx.history)
        if event_type:
            history = [h for h in history if h.get("message", {}).get("event") == event_type]
        if limit is not None and limit > 0:
            history = history[-limit:]
        return history

    # ── 待确认动作 ────────────────────────────────────────
    async def add_pending_action(
        self,
        client_id: str,
        action_id: str,
        action: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        """注册待确认动作。"""
        ctx = await self.get(client_id)
        if ctx is None:
            return None
        async with ctx.lock:
            return ctx.add_pending(action_id, action, timeout)

    async def confirm_action(
        self,
        client_id: str,
        action_id: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """确认动作并返回负载。"""
        ctx = await self.get(client_id)
        if ctx is None:
            return None
        async with ctx.lock:
            return ctx.confirm(action_id, result)

    async def get_pending_actions(self, client_id: str) -> list[dict[str, Any]]:
        """获取全部待确认动作。"""
        ctx = await self.get(client_id)
        if ctx is None:
            return []
        async with ctx.lock:
            return list(ctx.pending_confirmations.values())

    async def expire_pending_actions(self, client_id: str | None = None) -> list[dict[str, Any]]:
        """清理过期动作；client_id 为 None 时清理全部会话。"""
        expired: list[dict[str, Any]] = []
        if client_id is not None:
            ctx = await self.get(client_id)
            if ctx is None:
                return []
            async with ctx.lock:
                expired.extend(ctx.expire_pending())
            return expired

        async with self._lock:
            sessions = list(self._sessions.values())
        for ctx in sessions:
            async with ctx.lock:
                expired.extend(ctx.expire_pending())
        return expired

    # ── 订阅频道 ──────────────────────────────────────────
    async def subscribe(self, client_id: str, channels: list[str]) -> set[str] | None:
        """订阅频道，返回当前订阅集合。"""
        ctx = await self.get(client_id)
        if ctx is None:
            return None
        async with ctx.lock:
            ctx.subscriptions.update(channels)
            ctx.touch()
            return set(ctx.subscriptions)

    async def unsubscribe(self, client_id: str, channels: list[str]) -> set[str] | None:
        """取消订阅频道，返回当前订阅集合。"""
        ctx = await self.get(client_id)
        if ctx is None:
            return None
        async with ctx.lock:
            ctx.subscriptions.difference_update(channels)
            ctx.touch()
            return set(ctx.subscriptions)

    async def get_subscriptions(self, client_id: str) -> set[str] | None:
        ctx = await self.get(client_id)
        if ctx is None:
            return None
        async with ctx.lock:
            return set(ctx.subscriptions)

    # ── 元数据 ────────────────────────────────────────────
    async def update_metadata(self, client_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """合并更新会话元数据。"""
        ctx = await self.get(client_id)
        if ctx is None:
            return None
        async with ctx.lock:
            ctx.metadata.update(updates)
            ctx.touch()
            return dict(ctx.metadata)

    async def get_metadata(self, client_id: str) -> dict[str, Any] | None:
        ctx = await self.get(client_id)
        if ctx is None:
            return None
        async with ctx.lock:
            return dict(ctx.metadata)

    # ── 活跃时间 ──────────────────────────────────────────
    async def touch(self, client_id: str) -> bool:
        ctx = await self.get(client_id)
        if ctx is None:
            return False
        async with ctx.lock:
            ctx.touch()
        return True

    async def stale_sessions(self, threshold_seconds: float) -> list[str]:
        """返回超过阈值未活跃的 client_id 列表。"""
        now = time.time()
        stale: list[str] = []
        async with self._lock:
            sessions = list(self._sessions.items())
        for cid, ctx in sessions:
            async with ctx.lock:
                if now - ctx.last_seen > threshold_seconds:
                    stale.append(cid)
        return stale


# ── 全局单例 ──────────────────────────────────────────────
_store: SessionStore | None = None


def get_store() -> SessionStore:
    """获取全局 SessionStore 单例。"""
    global _store
    if _store is None:
        _store = SessionStore()
    return _store


def reset_store() -> None:
    """重置全局单例，主要用于测试。"""
    global _store
    _store = None
