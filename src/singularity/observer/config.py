"""Observer 配置与常量集中管理。

所有端口、采样间隔、告警阈值、白名单、历史长度等常量均从此模块导入，
避免在业务代码中散落魔法数值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── 网络服务 ──────────────────────────────────────────────
DEFAULT_HOST: str = "0.0.0.0"
DEFAULT_PORT: int = 8765

# WebSocket 连接限制
MAX_CLIENTS: int = 100
MAX_MESSAGE_SIZE: int = 64 * 1024  # 64KB

# 心跳保活
HEARTBEAT_INTERVAL: int = 30  # 秒
HEARTBEAT_TIMEOUT: int = HEARTBEAT_INTERVAL * 2  # 秒


# ── 采样与监控 ────────────────────────────────────────────
METRICS_SAMPLE_INTERVAL: float = 5.0  # 秒
SYSTEM_SAMPLE_INTERVAL: float = 10.0  # 秒

# 历史数据保留长度（数据点数量）
METRICS_HISTORY_LENGTH: int = 360  # 默认保留约 30 分钟（5s 间隔）
SYSTEM_HISTORY_LENGTH: int = 180  # 默认保留约 30 分钟（10s 间隔）
EVENT_HISTORY_LENGTH: int = 500


# ── 告警阈值 ──────────────────────────────────────────────
@dataclass(frozen=True)
class AlertThresholds:
    """告警阈值配置。"""

    cpu_percent: float = 80.0
    memory_percent: float = 85.0
    disk_percent: float = 90.0
    queue_depth: int = 50
    latency_ms: float = 1000.0
    error_rate: float = 0.05  # 5%


ALERT_THRESHOLDS = AlertThresholds()


# ── 白名单 / 过滤 ─────────────────────────────────────────
ALLOWED_EVENT_CHANNELS: frozenset[str] = frozenset({
    "metrics",
    "system",
    "alerts",
    "tasks",
    "logs",
    "heartbeat",
})

ALLOWED_ORIGINS: list[str] = field(default_factory=lambda: ["*"])


# ── 日志与调试 ────────────────────────────────────────────
DEFAULT_LOG_LEVEL: str = "INFO"
JSON_LOG_FORMAT: bool = False


# ── 运行时重载工具函数 ────────────────────────────────────
def as_dict() -> dict[str, Any]:
    """导出当前所有常量配置为字典，便于调试与前端展示。"""
    return {
        "network": {
            "host": DEFAULT_HOST,
            "port": DEFAULT_PORT,
            "max_clients": MAX_CLIENTS,
            "max_message_size": MAX_MESSAGE_SIZE,
            "heartbeat_interval": HEARTBEAT_INTERVAL,
            "heartbeat_timeout": HEARTBEAT_TIMEOUT,
        },
        "sampling": {
            "metrics_interval": METRICS_SAMPLE_INTERVAL,
            "system_interval": SYSTEM_SAMPLE_INTERVAL,
            "metrics_history": METRICS_HISTORY_LENGTH,
            "system_history": SYSTEM_HISTORY_LENGTH,
            "event_history": EVENT_HISTORY_LENGTH,
        },
        "alerts": ALERT_THRESHOLDS.__dict__,
        "whitelist": {
            "allowed_channels": sorted(ALLOWED_EVENT_CHANNELS),
            "allowed_origins": ALLOWED_ORIGINS,
        },
        "logging": {
            "level": DEFAULT_LOG_LEVEL,
            "json_format": JSON_LOG_FORMAT,
        },
    }
