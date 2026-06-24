"""WebSocket server for the observer package."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable, Coroutine

import websockets
from websockets.server import WebSocketServerProtocol

from observer.session import SessionManager

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5051

MessageHandler = Callable[[str, str | bytes], Coroutine[Any, Any, None]]


class ObserverServer:
    """WebSocket server that accepts clients and routes messages."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        session_manager: SessionManager | None = None,
        message_handler: MessageHandler | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.sessions = session_manager or SessionManager()
        self.message_handler = message_handler or self._default_message_handler
        self._server: websockets.Server | None = None
        self._stop_event: asyncio.Event | None = None

    @staticmethod
    async def _default_message_handler(client_id: str, message: str | bytes) -> None:
        """Echo received messages back to the sender."""
        logger.debug("Received message from %s: %r", client_id, message)

    async def _handle_client(self, websocket: WebSocketServerProtocol, path: str) -> None:
        """Handle an incoming WebSocket connection."""
        client_id = str(uuid.uuid4())
        logger.info("New WebSocket connection from %s (path=%s)", websocket.remote_address, path)
        await self.sessions.handle_connection(websocket, client_id, self.message_handler)

    async def start(self) -> None:
        """Start the WebSocket server and wait until stopped."""
        self._stop_event = asyncio.Event()
        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=10,
        )
        logger.info("Observer WebSocket server started on ws://%s:%d", self.host, self.port)
        await self._stop_event.wait()

    async def stop(self) -> None:
        """Stop the WebSocket server and close all connections."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.sessions.close_all()
        if self._stop_event is not None:
            self._stop_event.set()
        logger.info("Observer WebSocket server stopped")

    def run(self) -> None:
        """Run the server using the default event loop."""
        try:
            asyncio.run(self.start())
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received, shutting down")
            asyncio.run(self.stop())


def main() -> None:
    """CLI entry point to start the observer WebSocket server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    server = ObserverServer()
    server.run()


if __name__ == "__main__":
    main()
