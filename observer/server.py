"""WebSocket server for the observer package.

Provides a websockets-based server that listens on port 5051,
manages client connections via SessionManager, and supports
message routing between clients and an optional message handler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from typing import Any, Callable, Coroutine

import websockets

from observer.session import SessionManager

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5051

# Type alias for an external message handler callback
MessageHandler = Callable[[str, str | bytes], Coroutine[Any, Any, None]]


class ObserverServer:
    """WebSocket server that manages observer client connections.

    The server listens on a configurable host/port, registers each
    connecting client with the SessionManager, and routes incoming
    messages through an optional handler callback.

    Parameters
    ----------
    host : str
        Bind address (default ``0.0.0.0``).
    port : int
        Listen port (default ``5051``).
    message_handler : MessageHandler | None
        An async callable ``(client_id, message) -> None`` invoked for
        every incoming message.  When *None* the server echoes messages
        back to the sender.
    session_manager : SessionManager | None
        A pre-existing SessionManager instance.  If *None* a new one is
        created internally.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        message_handler: MessageHandler | None = None,
        session_manager: SessionManager | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.session_manager = session_manager or SessionManager()
        self._message_handler = message_handler
        self._server: websockets.WebSocketServer | None = None
        self._stop_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        """Return True while the server is actively listening."""
        return self._server is not None and self._server.is_serving()

    @property
    def client_count(self) -> int:
        """Return the number of currently connected clients."""
        return self.session_manager.count

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(self, websocket: websockets.ServerConnection) -> None:
        """Handle a single WebSocket client lifecycle."""
        # Derive a client_id from the remote address or a query parameter
        client_id = self._extract_client_id(websocket)
        logger.info("New connection from %s (assigned id: %s)", websocket.remote_address, client_id)

        # Send a welcome message so the client knows the connection is live
        try:
            welcome = json.dumps({"type": "welcome", "client_id": client_id})
            await websocket.send(welcome)
        except websockets.exceptions.ConnectionClosed:
            return

        await self.session_manager.handle_connection(
            websocket,
            client_id,
            handler=self._on_message,
        )

    async def _on_message(self, client_id: str, message: str | bytes) -> None:
        """Route an incoming message to the configured handler or echo it."""
        logger.debug("Message from %s: %.200s", client_id, message)
        if self._message_handler is not None:
            await self._message_handler(client_id, message)
        else:
            # Default echo behaviour
            echo = json.dumps({"type": "echo", "from": client_id, "data": message if isinstance(message, str) else message.decode(errors="replace")})
            await self.session_manager.send(client_id, echo)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_client_id(websocket: websockets.ServerConnection) -> str:
        """Derive a unique client identifier from the connection.

        Checks for a ``client_id`` query parameter first, then falls
        back to ``host:port`` of the remote address.
        """
        try:
            request = websocket.request
            if request is not None:
                path = request.path or ""
                # Simple query-string parse for ?client_id=xxx
                if "?" in path:
                    query_string = path.split("?", 1)[1]
                    params: dict[str, str] = {}
                    for part in query_string.split("&"):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            params[k] = v
                    if "client_id" in params and params["client_id"]:
                        return params["client_id"]
        except Exception:  # noqa: BLE001
            pass

        # Fallback: remote address
        remote = websocket.remote_address
        if remote:
            return f"{remote[0]}:{remote[1]}"
        return f"client-{id(websocket)}"

    # ------------------------------------------------------------------
    # Public API – send / broadcast
    # ------------------------------------------------------------------

    async def send(self, client_id: str, message: str | bytes) -> bool:
        """Send a message to a specific connected client."""
        return await self.session_manager.send(client_id, message)

    async def broadcast(self, message: str | bytes) -> dict[str, bool]:
        """Broadcast a message to every connected client."""
        return await self.session_manager.broadcast(message)

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start listening for WebSocket connections (non-blocking)."""
        if self.is_running:
            logger.warning("Server is already running on %s:%d", self.host, self.port)
            return

        self._stop_event.clear()
        self._server = await websockets.serve(
            self._handle_connection,
            self.host,
            self.port,
            ping_interval=30,
            ping_timeout=10,
        )
        logger.info("ObserverServer started on ws://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Gracefully shut down the server and close all client connections."""
        if self._server is None:
            return

        logger.info("ObserverServer shutting down…")
        self._stop_event.set()

        # Close all client sessions
        await self.session_manager.close_all()

        # Stop accepting new connections
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("ObserverServer stopped")

    async def serve_forever(self) -> None:
        """Start the server and block until a stop signal is received."""
        await self.start()

        # Install signal handlers for graceful shutdown (Unix only)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                # Windows does not support add_signal_handler
                pass

        try:
            await self._stop_event.wait()
        finally:
            await self.stop()


# ------------------------------------------------------------------
# Module-level convenience
# ------------------------------------------------------------------

_server_instance: ObserverServer | None = None


def get_server() -> ObserverServer | None:
    """Return the current global server instance, if any."""
    return _server_instance


async def start_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    message_handler: MessageHandler | None = None,
    session_manager: SessionManager | None = None,
) -> ObserverServer:
    """Create, start, and return a new ObserverServer."""
    global _server_instance
    server = ObserverServer(
        host=host,
        port=port,
        message_handler=message_handler,
        session_manager=session_manager,
    )
    await server.start()
    _server_instance = server
    return server


async def stop_server() -> None:
    """Stop the global server instance if one is running."""
    global _server_instance
    if _server_instance is not None:
        await _server_instance.stop()
        _server_instance = None


def main() -> None:
    """CLI entry-point: run the observer WebSocket server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting ObserverServer via CLI…")
    asyncio.run(ObserverServer().serve_forever())


if __name__ == "__main__":
    main()
