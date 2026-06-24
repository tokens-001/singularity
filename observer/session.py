"""Client session management for the observer WebSocket server."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import websockets
from websockets.protocol import State

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages WebSocket client connections and supports broadcast/unicast."""

    def __init__(self) -> None:
        self._connections: dict[str, websockets.ServerConnection] = {}
        self._lock = asyncio.Lock()

    @property
    def connections(self) -> dict[str, websockets.ServerProtocol]:
        """Return a shallow copy of the current connection mapping."""
        return dict(self._connections)

    @property
    def count(self) -> int:
        """Return the number of currently connected clients."""
        return len(self._connections)

    async def add(self, client_id: str, websocket: websockets.ServerProtocol) -> None:
        """Register a client connection, closing any previous one with the same ID."""
        async with self._lock:
            existing = self._connections.get(client_id)
            if existing is not None and existing.open:
                logger.warning("Replacing existing connection for client %s", client_id)
                await existing.close()
            self._connections[client_id] = websocket
        logger.info("Client registered: %s (total: %d)", client_id, len(self._connections))

    async def remove(self, client_id: str) -> None:
        """Unregister a client connection."""
        async with self._lock:
            ws = self._connections.pop(client_id, None)
        if ws is not None:
            if ws.state == websockets.protocol.State.OPEN:
                await ws.close()
            logger.info("Client unregistered: %s (total: %d)", client_id, len(self._connections))

    async def get(self, client_id: str) -> websockets.ServerProtocol | None:
        """Return the websocket for a given client ID if connected."""
        async with self._lock:
            ws = self._connections.get(client_id)
            if ws is None or not ws.state == websockets.protocol.State.OPEN:
                return None
            return ws

    async def send(self, client_id: str, message: str | bytes) -> bool:
        """Send a message to a single client. Returns True if sent."""
        ws = await self.get(client_id)
        if ws is None:
            logger.debug("Cannot send to %s: not connected", client_id)
            return False
        try:
            await ws.send(message)
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Connection closed while sending to %s", client_id)
            await self.remove(client_id)
            return False

    async def broadcast(self, message: str | bytes) -> dict[str, bool]:
        """Send a message to all connected clients."""
        snapshot = self.connections
        results: dict[str, bool] = {}
        for client_id in snapshot:
            results[client_id] = await self.send(client_id, message)
        return results

    async def broadcast_filter(
        self,
        message: str | bytes,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
    ) -> dict[str, bool]:
        """Broadcast to a subset of clients."""
        snapshot = self.connections
        targets = set(snapshot.keys())
        if include is not None:
            targets &= include
        if exclude is not None:
            targets -= exclude
        results: dict[str, bool] = {}
        for client_id in targets:
            results[client_id] = await self.send(client_id, message)
        return results

    async def close_all(self) -> None:
        """Close all managed connections and clear the registry."""
        async with self._lock:
            clients = list(self._connections.items())
            self._connections.clear()
        for client_id, ws in clients:
            try:
                if ws.state == websockets.protocol.State.OPEN:
                    await ws.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing connection for %s: %s", client_id, exc)
        logger.info("All client connections closed")

    async def handle_connection(
        self,
        websocket: websockets.ServerProtocol,
        client_id: str,
        handler: Any | None = None,
    ) -> None:
        """Run a connection lifecycle: register, optionally route messages, unregister."""
        await self.add(client_id, websocket)
        try:
            async for message in websocket:
                if handler is not None:
                    try:
                        await handler(client_id, message)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Message handler error for %s: %s", client_id, exc)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Connection closed for %s", client_id)
        finally:
            await self.remove(client_id)
