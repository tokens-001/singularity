"""Observer WebSocket server.

A lightweight websocket server that:
- listens on bridge.py:5051
- accepts multiple concurrent clients
- echoes incoming messages back to the sender
- supports active broadcasts via :meth:`broadcast`
- supports targeted pushes via :meth:`send`
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger("observer.server")

Handler = Callable[[WebSocketServerProtocol, dict[str, Any]], Awaitable[None]]


class ObserverServer:
    """WebSocket server singleton/dispatcher for agent observation bridge."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5051) -> None:
        self.host = host
        self.port = port
        self.clients: set[WebSocketServerProtocol] = set()
        self.handlers: dict[str, Handler] = {}
        self._server: websockets.server.Serve | None = None
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Client lifecycle
    # ------------------------------------------------------------------ #
    async def _register(self, ws: WebSocketServerProtocol) -> None:
        self.clients.add(ws)
        logger.info("client connected: %s (total=%d)", ws.remote_address, len(self.clients))

    async def _unregister(self, ws: WebSocketServerProtocol) -> None:
        self.clients.discard(ws)
        logger.info("client disconnected: %s (total=%d)", ws.remote_address, len(self.clients))

    async def _handle_client(self, ws: WebSocketServerProtocol, path: str) -> None:  # noqa: ARG002
        await self._register(ws)
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    logger.warning("binary messages are not supported")
                    continue
                await self._on_message(ws, raw)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("connection closed")
        finally:
            await self._unregister(ws)

    # ------------------------------------------------------------------ #
    # Message dispatch
    # ------------------------------------------------------------------ #
    async def _on_message(self, ws: WebSocketServerProtocol, raw: str) -> None:
        logger.debug("received: %s", raw)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await self.send(ws, {"type": "error", "message": "invalid json"})
            return

        msg_type = payload.get("type")
        handler = self.handlers.get(msg_type)
        if handler:
            await handler(ws, payload)
        else:
            # default echo behaviour for skeleton
            await self.send(ws, {"type": "echo", "payload": payload})

    def on(self, msg_type: str) -> Callable[[Handler], Handler]:
        """Register a handler for a given message type."""

        def decorator(fn: Handler) -> Handler:
            self.handlers[msg_type] = fn
            return fn

        return decorator

    # ------------------------------------------------------------------ #
    # Active push / broadcast
    # ------------------------------------------------------------------ #
    async def send(self, ws: WebSocketServerProtocol, message: dict[str, Any]) -> None:
        """Push a JSON message to a single client."""
        if ws.closed:
            return
        try:
            await ws.send(json.dumps(message))
        except websockets.exceptions.ConnectionClosed:
            self.clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> int:
        """Broadcast a JSON message to every connected client.

        Returns the number of clients that received the message.
        """
        if not self.clients:
            return 0
        payload = json.dumps(message)
        results = await asyncio.gather(
            *[self._send_one(client, payload) for client in list(self.clients)],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def _send_one(self, ws: WebSocketServerProtocol, payload: str) -> bool:
        if ws.closed:
            self.clients.discard(ws)
            return False
        try:
            await ws.send(payload)
        except websockets.exceptions.ConnectionClosed:
            self.clients.discard(ws)
            return False
        return True

    # ------------------------------------------------------------------ #
    # Server lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Start the server and block until :meth:`stop` is called."""
        logger.info("starting observer server on ws://%s:%d", self.host, self.port)
        self._server = websockets.serve(self._handle_client, self.host, self.port)
        await self._server
        await self._stop_event.wait()

    async def stop(self) -> None:
        """Close all clients and stop the server."""
        logger.info("stopping observer server")
        # close existing connections gracefully
        await asyncio.gather(
            *[client.close() for client in list(self.clients)],
            return_exceptions=True,
        )
        self.clients.clear()
        self._stop_event.set()

    def run(self) -> None:
        """Synchronous entry point."""
        try:
            asyncio.run(self.start())
        except KeyboardInterrupt:
            asyncio.run(self.stop())


# ---------------------------------------------------------------------- #
# Optional thin bridge compatibility layer
# ---------------------------------------------------------------------- #
async def start_bridge(host: str = "127.0.0.1", port: int = 5051) -> ObserverServer:
    """Create and start an observer server (non-blocking in running loop)."""
    server = ObserverServer(host=host, port=port)
    server._server = websockets.serve(server._handle_client, host, port)
    await server._server
    logger.info("bridge observer listening on ws://%s:%d", host, port)
    return server


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ObserverServer().run()
