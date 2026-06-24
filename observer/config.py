"""Centralized configuration and constants for the observer package.

All tunable operational parameters—network ports, sampling intervals,
alert thresholds, whitelist rules, and history windows—live here so that
runtime behaviour can be adjusted from a single location.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Network / WebSocket endpoints
# --------------------------------------------------------------------------- #

OBSERVER_HOST: str = "127.0.0.1"
"""Default host interface the observer WebSocket server binds to."""

OBSERVER_PORT: int = 5051
"""Default TCP port the observer WebSocket server listens on."""

BRIDGE_PORT: int = OBSERVER_PORT
"""Port exposed to bridge.py for backwards compatibility.

This must remain identical to :data:`OBSERVER_PORT`; defining it as an
alias ensures the two cannot drift apart when the observer port is
changed.
"""

# --------------------------------------------------------------------------- #
# Sampling and timing
# --------------------------------------------------------------------------- #

SAMPLE_INTERVAL_SECONDS: float = 1.0
"""Default interval between observation samples."""

HEARTBEAT_INTERVAL_SECONDS: float = 30.0
"""Interval at which the server emits keep-alive heartbeats."""

RECONNECT_INTERVAL_SECONDS: float = 5.0
"""Interval between client reconnection attempts."""

# --------------------------------------------------------------------------- #
# Alert thresholds
# --------------------------------------------------------------------------- #

CPU_THRESHOLD_PERCENT: float = 90.0
"""CPU usage percentage that triggers an alert."""

MEMORY_THRESHOLD_PERCENT: float = 85.0
"""Memory usage percentage that triggers an alert."""

DISK_THRESHOLD_PERCENT: float = 90.0
"""Disk usage percentage that triggers an alert."""

LATENCY_THRESHOLD_MS: float = 1000.0
"""Round-trip latency in milliseconds that triggers an alert."""

ERROR_RATE_THRESHOLD: float = 0.05
"""Error rate (0.0–1.0) above which an alert is raised."""

# --------------------------------------------------------------------------- #
# Whitelist / filtering
# --------------------------------------------------------------------------- #

WHITELISTED_ORIGINS: tuple[str, ...] = (
    "http://localhost",
    "http://127.0.0.1",
)
"""Allowed origins for incoming WebSocket connections."""

WHITELISTED_MESSAGE_TYPES: tuple[str, ...] = (
    "echo",
    "ping",
    "subscribe",
    "unsubscribe",
    "status",
)
"""Message types accepted by the observer dispatcher without extra auth."""

# --------------------------------------------------------------------------- #
# History / buffering
# --------------------------------------------------------------------------- #

MAX_HISTORY_LENGTH: int = 1000
"""Maximum number of samples retained per metric series."""

MAX_ALERT_HISTORY: int = 100
"""Maximum number of alert records kept for replay."""

MAX_LOG_HISTORY: int = 500
"""Maximum number of recent log entries buffered."""

# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #

DEFAULT_LOG_LEVEL: str = "INFO"
"""Default logging level for the observer package."""

JSON_INDENT: int | None = None
"""Indentation for JSON payloads; None produces compact output."""
