"""Alert Manager — 告警分级（P0/P1/P2）与滑动窗口去重。

职责：
  - 定义告警严重等级（P0 紧急 / P1 警告 / P2 提示）
  - 滑动窗口去重：同一指纹在窗口内仅触发一次，避免刷屏
  - 告警日志、历史缓存、统计
  - 可接入 observer.server 的广播通道，实时推送给 WebSocket 客户端

使用示例::

    from observer.alert_manager import AlertLevel, AlertManager, Alert

    mgr = AlertManager()
    mgr.fire(Alert(
        level=AlertLevel.P1,
        title="CPU 过高",
        source="system.cpu",
        detail={"usage_pct": 95.2, "threshold": 90},
    ))
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from observer.config import (
    CPU_THRESHOLD_PERCENT,
    DISK_THRESHOLD_PERCENT,
    ERROR_RATE_THRESHOLD,
    LATENCY_THRESHOLD_MS,
    MEMORY_THRESHOLD_PERCENT,
    MAX_ALERT_HISTORY,
)

logger = logging.getLogger("observer.alert")

# ---------------------------------------------------------------------------
# 告警等级
# ---------------------------------------------------------------------------


class AlertLevel(int, Enum):
    """告警严重等级。

    - P0: 紧急 —— 需要立即人工介入（服务宕机、数据损坏等）
    - P1: 警告 —— 需关注，可能快速恶化（CPU 长期打满、内存泄漏等）
    - P2: 提示 —— 信息性告警，常规记录（配置变更、慢查询等）
    """

    P0 = 0  # 紧急
    P1 = 1  # 警告
    P2 = 2  # 提示

    @property
    def label(self) -> str:
        return {AlertLevel.P0: "P0-紧急", AlertLevel.P1: "P1-警告", AlertLevel.P2: "P2-提示"}[self]

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# 告警数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    """一条告警。

    Args:
        level: 严重等级 P0/P1/P2。
        title: 简短标题，用于人类阅读与聚合。
        source: 告警来源标识，如 ``"system.cpu"``、``"executor.timeout"``。
        detail: 任意结构化详情。
        fingerprint: 去重指纹；若为空则自动从 ``source + title`` 生成。
        timestamp: 发生时间（epoch 秒），默认当前时间。
    """

    level: AlertLevel
    title: str
    source: str
    detail: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", f"{self.source}::{self.title}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.name,
            "level_label": self.level.label,
            "title": self.title,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "detail": self.detail,
            "timestamp": self.timestamp,
            "timestamp_iso": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(self.timestamp)
            ),
        }


# ---------------------------------------------------------------------------
# 滑动窗口去重器
# ---------------------------------------------------------------------------


class _SlidingWindowDedup:
    """基于时间戳 + 指纹的滑动窗口去重器。

    内部维护一个 OrderedDict，按时间顺序保存每个指纹最近一次
    触发的时间戳。每次查询时先驱逐窗口外的过期条目。
    """

    __slots__ = ("_window_seconds", "_entries")

    def __init__(self, window_seconds: float) -> None:
        self._window_seconds = window_seconds
        self._entries: OrderedDict[str, float] = OrderedDict()

    def _evict(self, now: float) -> None:
        """驱逐窗口外的过期指纹。"""
        cutoff = now - self._window_seconds
        stale: list[str] = []
        for fp, ts in self._entries.items():
            if ts < cutoff:
                stale.append(fp)
            else:
                break
        for fp in stale:
            del self._entries[fp]

    def should_fire(self, fingerprint: str, now: float | None = None) -> bool:
        """判断该指纹是否应触发（窗口内首次出现）。"""
        now = now or time.time()
        self._evict(now)

        if fingerprint in self._entries:
            # 窗口内已存在，去重命中
            self._entries.move_to_end(fingerprint)
            return False

        self._entries[fingerprint] = now
        # 防止无限增长
        while len(self._entries) > MAX_ALERT_HISTORY * 2:
            self._entries.popitem(last=False)
        return True

    def reset(self) -> None:
        self._entries.clear()

    @property
    def count(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# AlertManager
# ---------------------------------------------------------------------------


class AlertManager:
    """告警管理器 —— 分级校验、滑动窗口去重、历史存储、广播。

    Args:
        dedup_window_p0: P0 级去重窗口（秒）。默认 60 秒，即同一告警
            每分钟最多触发一次。
        dedup_window_p1: P1 级去重窗口（秒）。默认 300 秒（5 分钟）。
        dedup_window_p2: P2 级去重窗口（秒）。默认 900 秒（15 分钟）。
        max_history: 最多缓存的告警记录条数。
        broadcast_fn: 可选广播回调，签名 ``async def fn(alert: Alert)``，
            用于将告警推送到 WebSocket 等通道。
    """

    # ------------------------------------------------------------------
    # 滑动窗口默认值（秒）
    # ------------------------------------------------------------------
    DEFAULT_WINDOW_P0: float = 60.0
    DEFAULT_WINDOW_P1: float = 300.0
    DEFAULT_WINDOW_P2: float = 900.0

    def __init__(
        self,
        dedup_window_p0: float | None = None,
        dedup_window_p1: float | None = None,
        dedup_window_p2: float | None = None,
        max_history: int | None = None,
        broadcast_fn: Callable[[Alert], Any] | None = None,
    ) -> None:
        self._max_history = max_history or MAX_ALERT_HISTORY
        self._broadcast_fn = broadcast_fn

        # 每个等级独立的滑动窗口去重器
        self._dedup: dict[AlertLevel, _SlidingWindowDedup] = {
            AlertLevel.P0: _SlidingWindowDedup(
                dedup_window_p0 if dedup_window_p0 is not None else self.DEFAULT_WINDOW_P0
            ),
            AlertLevel.P1: _SlidingWindowDedup(
                dedup_window_p1 if dedup_window_p1 is not None else self.DEFAULT_WINDOW_P1
            ),
            AlertLevel.P2: _SlidingWindowDedup(
                dedup_window_p2 if dedup_window_p2 is not None else self.DEFAULT_WINDOW_P2
            ),
        }

        # 告警历史环形缓冲区
        self._history: list[dict[str, Any]] = []

        # 统计
        self._stats: dict[str, int] = {
            "fired": 0,       # 实际触发的告警数
            "suppressed": 0,  # 被去重抑制的数量
            "p0": 0,
            "p1": 0,
            "p2": 0,
        }

        # 协程锁保证线程/协程安全
        self._lock = asyncio.Lock()

        logger.info(
            "AlertManager 初始化: P0窗口=%.0fs P1窗口=%.0fs P2窗口=%.0fs 最大历史=%d",
            self._dedup[AlertLevel.P0]._window_seconds,
            self._dedup[AlertLevel.P1]._window_seconds,
            self._dedup[AlertLevel.P2]._window_seconds,
            self._max_history,
        )

    # ------------------------------------------------------------------
    # 核心：触发告警
    # ------------------------------------------------------------------

    async def fire(self, alert: Alert) -> bool:
        """触发一条告警。

        如果该告警指纹在对应等级的滑动窗口内已经触发过，则本次被抑制，
        仅增加 suppressed 计数。

        Returns:
            True  —— 告警实际触发并记录。
            False —— 窗口内重复，被抑制。
        """
        async with self._lock:
            return self._fire_sync(alert)

    def fire_sync(self, alert: Alert) -> bool:
        """同步版本 fire()，用于非异步上下文。"""
        return self._fire_sync(alert)

    def _fire_sync(self, alert: Alert) -> bool:
        """实际触发逻辑（调用方保证线程安全）。"""
        dedup = self._dedup[alert.level]

        if not dedup.should_fire(alert.fingerprint, alert.timestamp):
            self._stats["suppressed"] += 1
            logger.debug(
                "告警抑制 (重复): %s | %s | 指纹=%s",
                alert.level.label,
                alert.title,
                alert.fingerprint,
            )
            return False

        # 通过去重 → 记录
        record = alert.to_dict()
        self._history.append(record)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        self._stats["fired"] += 1
        level_key = alert.level.name.lower()
        self._stats[level_key] += 1

        logger.warning(
            "⚠ 告警触发: %s | %s | %s | detail=%s",
            alert.level.label,
            alert.title,
            alert.source,
            alert.detail,
        )

        # 异步广播（如果配置了回调，在同步上下文中通过 create_task 调度）
        if self._broadcast_fn is not None:
            try:
                result = self._broadcast_fn(alert)
                # 如果返回的是协程，安排执行
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(result)
                    except RuntimeError:
                        # 没有运行中的事件循环，忽略
                        pass
            except Exception:
                logger.exception("告警广播回调异常")

        return True

    async def fire_many(self, alerts: list[Alert]) -> dict[str, int]:
        """批量触发告警，返回 {fired, suppressed} 计数。"""
        fired = suppressed = 0
        async with self._lock:
            for alert in alerts:
                if self._fire_sync(alert):
                    fired += 1
                else:
                    suppressed += 1
        return {"fired": fired, "suppressed": suppressed}

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    async def get_history(
        self,
        limit: int | None = None,
        level: AlertLevel | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询告警历史，支持按等级、来源过滤。

        Args:
            limit: 返回最近 N 条。
            level: 按等级过滤，None 表示全部。
            source: 按来源前缀过滤，None 表示全部。

        Returns:
            告警记录列表，时间升序（最近的在末尾）。
        """
        async with self._lock:
            history = list(self._history)

        if level is not None:
            level_name = level.name
            history = [r for r in history if r["level"] == level_name]
        if source is not None:
            history = [r for r in history if r["source"].startswith(source)]

        if limit is not None and limit > 0:
            history = history[-limit:]
        return history

    async def get_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取最近的 N 条告警。"""
        return await self.get_history(limit=limit)

    async def get_stats(self) -> dict[str, int]:
        """获取告警统计。"""
        async with self._lock:
            return dict(self._stats)

    async def get_dedup_state(self) -> dict[str, int]:
        """获取各等级去重器当前指纹数（调试用）。"""
        async with self._lock:
            return {
                level.name: dedup.count for level, dedup in self._dedup.items()
            }

    # ------------------------------------------------------------------
    # 管理接口
    # ------------------------------------------------------------------

    async def reset_dedup(self, level: AlertLevel | None = None) -> None:
        """重置指定等级（或全部）的去重窗口。"""
        async with self._lock:
            if level is None:
                for dedup in self._dedup.values():
                    dedup.reset()
                logger.info("已重置全部去重窗口")
            else:
                self._dedup[level].reset()
                logger.info("已重置 %s 去重窗口", level.label)

    async def reset_stats(self) -> None:
        """重置统计计数器。"""
        async with self._lock:
            for key in self._stats:
                self._stats[key] = 0

    async def clear_history(self) -> None:
        """清空告警历史。"""
        async with self._lock:
            self._history.clear()

    # ------------------------------------------------------------------
    # 便利方法：从阈值判断生成告警
    # ------------------------------------------------------------------

    async def check_cpu(
        self, usage_pct: float, threshold: float | None = None
    ) -> Alert | None:
        """检查 CPU 使用率，超标则返回 Alert。"""
        threshold = threshold if threshold is not None else CPU_THRESHOLD_PERCENT
        if usage_pct < threshold:
            return None
        # 根据超标幅度分级
        if usage_pct >= 98:
            level = AlertLevel.P0
        elif usage_pct >= threshold + 5:
            level = AlertLevel.P1
        else:
            level = AlertLevel.P2
        return Alert(
            level=level,
            title="CPU 使用率过高",
            source="system.cpu",
            detail={"usage_pct": usage_pct, "threshold": threshold},
        )

    async def check_memory(
        self, usage_pct: float, threshold: float | None = None
    ) -> Alert | None:
        """检查内存使用率。"""
        threshold = threshold if threshold is not None else MEMORY_THRESHOLD_PERCENT
        if usage_pct < threshold:
            return None
        if usage_pct >= 98:
            level = AlertLevel.P0
        elif usage_pct >= threshold + 5:
            level = AlertLevel.P1
        else:
            level = AlertLevel.P2
        return Alert(
            level=level,
            title="内存使用率过高",
            source="system.memory",
            detail={"usage_pct": usage_pct, "threshold": threshold},
        )

    async def check_disk(
        self, usage_pct: float, threshold: float | None = None
    ) -> Alert | None:
        """检查磁盘使用率。"""
        threshold = threshold if threshold is not None else DISK_THRESHOLD_PERCENT
        if usage_pct < threshold:
            return None
        if usage_pct >= 98:
            level = AlertLevel.P0
        elif usage_pct >= threshold + 5:
            level = AlertLevel.P1
        else:
            level = AlertLevel.P2
        return Alert(
            level=level,
            title="磁盘使用率过高",
            source="system.disk",
            detail={"usage_pct": usage_pct, "threshold": threshold},
        )

    async def check_latency(
        self, latency_ms: float, threshold: float | None = None
    ) -> Alert | None:
        """检查延迟。"""
        threshold = threshold if threshold is not None else LATENCY_THRESHOLD_MS
        if latency_ms < threshold:
            return None
        factor = latency_ms / threshold
        if factor >= 5:
            level = AlertLevel.P0
        elif factor >= 2:
            level = AlertLevel.P1
        else:
            level = AlertLevel.P2
        return Alert(
            level=level,
            title="延迟过高",
            source="system.latency",
            detail={"latency_ms": latency_ms, "threshold_ms": threshold},
        )

    async def check_error_rate(
        self, error_rate: float, threshold: float | None = None
    ) -> Alert | None:
        """检查错误率。"""
        threshold = threshold if threshold is not None else ERROR_RATE_THRESHOLD
        if error_rate < threshold:
            return None
        factor = error_rate / max(threshold, 0.001)
        if factor >= 5:
            level = AlertLevel.P0
        elif factor >= 2:
            level = AlertLevel.P1
        else:
            level = AlertLevel.P2
        return Alert(
            level=level,
            title="错误率过高",
            source="system.error_rate",
            detail={"error_rate": error_rate, "threshold": threshold},
        )

    async def check_and_fire(
        self, alert: Alert | None
    ) -> bool:
        """检查 Alert 是否为 None，非空则 fire。返回是否触发。"""
        if alert is None:
            return False
        return await self.fire(alert)


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_manager: AlertManager | None = None


def get_alert_manager() -> AlertManager:
    """获取全局 AlertManager 单例。"""
    global _manager
    if _manager is None:
        _manager = AlertManager()
    return _manager


def init_alert_manager(
    dedup_window_p0: float | None = None,
    dedup_window_p1: float | None = None,
    dedup_window_p2: float | None = None,
    max_history: int | None = None,
    broadcast_fn: Callable[[Alert], Any] | None = None,
) -> AlertManager:
    """初始化全局 AlertManager 单例。"""
    global _manager
    _manager = AlertManager(
        dedup_window_p0=dedup_window_p0,
        dedup_window_p1=dedup_window_p1,
        dedup_window_p2=dedup_window_p2,
        max_history=max_history,
        broadcast_fn=broadcast_fn,
    )
    return _manager


def reset_alert_manager() -> None:
    """重置全局单例，主要用于测试。"""
    global _manager
    _manager = None
